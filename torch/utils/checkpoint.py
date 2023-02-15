import torch
import warnings
import weakref
from weakref import ReferenceType
from typing import Any, Iterable, List, Tuple, Dict, Optional, DefaultDict, NamedTuple
from collections import defaultdict
import uuid
import contextlib

__all__ = [
    "checkpoint", "checkpoint_sequential", "CheckpointFunction",
    "check_backward_validity", "detach_variable", "get_device_states",
    "set_device_states",
]

def detach_variable(inputs: Tuple[Any, ...]) -> Tuple[torch.Tensor, ...]:
    if isinstance(inputs, tuple):
        out = []
        for inp in inputs:
            if not isinstance(inp, torch.Tensor):
                out.append(inp)
                continue

            x = inp.detach()
            x.requires_grad = inp.requires_grad
            out.append(x)
        return tuple(out)
    else:
        raise RuntimeError(
            "Only tuple of tensors is supported. Got Unsupported input type: ", type(inputs).__name__)


def check_backward_validity(inputs: Iterable[Any]) -> None:
    if not any(inp.requires_grad for inp in inputs if isinstance(inp, torch.Tensor)):
        warnings.warn("None of the inputs have requires_grad=True. Gradients will be None")


# We can't know if the run_fn will internally move some args to different devices,
# which would require logic to preserve rng states for those devices as well.
# We could paranoically stash and restore ALL the rng states for all visible devices,
# but that seems very wasteful for most cases.  Compromise:  Stash the RNG state for
# the device of all Tensor args.
#
# To consider:  maybe get_device_states and set_device_states should reside in torch/random.py?
def get_device_states(*args) -> Tuple[List[int], List[torch.Tensor]]:
    # This will not error out if "arg" is a CPU tensor or a non-tensor type because
    # the conditionals short-circuit.
    fwd_gpu_devices = list({arg.get_device() for arg in args
                            if isinstance(arg, torch.Tensor) and arg.is_cuda})

    fwd_gpu_states = []
    for device in fwd_gpu_devices:
        with torch.cuda.device(device):
            fwd_gpu_states.append(torch.cuda.get_rng_state())

    return fwd_gpu_devices, fwd_gpu_states


def set_device_states(devices, states) -> None:
    for device, state in zip(devices, states):
        with torch.cuda.device(device):
            torch.cuda.set_rng_state(state)

def _get_autocast_kwargs():
    gpu_autocast_kwargs = {"enabled": torch.is_autocast_enabled(),
                           "dtype": torch.get_autocast_gpu_dtype(),
                           "cache_enabled": torch.is_autocast_cache_enabled()}

    cpu_autocast_kwargs = {"enabled": torch.is_autocast_cpu_enabled(),
                           "dtype": torch.get_autocast_cpu_dtype(),
                           "cache_enabled": torch.is_autocast_cache_enabled()}

    return gpu_autocast_kwargs, cpu_autocast_kwargs

class CheckpointFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, run_function, preserve_rng_state, *args):
        check_backward_validity(args)
        ctx.run_function = run_function
        ctx.preserve_rng_state = preserve_rng_state
        # Accommodates the (remote) possibility that autocast is enabled for cpu AND gpu.
        ctx.gpu_autocast_kwargs, ctx.cpu_autocast_kwargs = _get_autocast_kwargs()
        if preserve_rng_state:
            ctx.fwd_cpu_state = torch.get_rng_state()
            # Don't eagerly initialize the cuda context by accident.
            # (If the user intends that the context is initialized later, within their
            # run_function, we SHOULD actually stash the cuda state here.  Unfortunately,
            # we have no way to anticipate this will happen before we run the function.)
            ctx.had_cuda_in_fwd = False
            if torch.cuda._initialized:
                ctx.had_cuda_in_fwd = True
                ctx.fwd_gpu_devices, ctx.fwd_gpu_states = get_device_states(*args)

        # Save non-tensor inputs in ctx, keep a placeholder None for tensors
        # to be filled out during the backward.
        ctx.inputs = []
        ctx.tensor_indices = []
        tensor_inputs = []
        for i, arg in enumerate(args):
            if torch.is_tensor(arg):
                tensor_inputs.append(arg)
                ctx.tensor_indices.append(i)
                ctx.inputs.append(None)
            else:
                ctx.inputs.append(arg)

        ctx.save_for_backward(*tensor_inputs)

        with torch.no_grad():
            outputs = run_function(*args)
        return outputs

    @staticmethod
    def backward(ctx, *args):
        if not torch.autograd._is_checkpoint_valid():
            raise RuntimeError(
                "Checkpointing is not compatible with .grad() or when an `inputs` parameter"
                " is passed to .backward(). Please use .backward() and do not pass its `inputs`"
                " argument.")
        # Copy the list to avoid modifying original list.
        inputs = list(ctx.inputs)
        tensor_indices = ctx.tensor_indices
        tensors = ctx.saved_tensors

        # Fill in inputs with appropriate saved tensors.
        for i, idx in enumerate(tensor_indices):
            inputs[idx] = tensors[i]

        # Stash the surrounding rng state, and mimic the state that was
        # present at this time during forward.  Restore the surrounding state
        # when we're done.
        rng_devices = []
        if ctx.preserve_rng_state and ctx.had_cuda_in_fwd:
            rng_devices = ctx.fwd_gpu_devices
        with torch.random.fork_rng(devices=rng_devices, enabled=ctx.preserve_rng_state):
            if ctx.preserve_rng_state:
                torch.set_rng_state(ctx.fwd_cpu_state)
                if ctx.had_cuda_in_fwd:
                    set_device_states(ctx.fwd_gpu_devices, ctx.fwd_gpu_states)
            detached_inputs = detach_variable(tuple(inputs))
            with torch.enable_grad(), \
                 torch.cuda.amp.autocast(**ctx.gpu_autocast_kwargs), \
                 torch.cpu.amp.autocast(**ctx.cpu_autocast_kwargs):
                outputs = ctx.run_function(*detached_inputs)

        if isinstance(outputs, torch.Tensor):
            outputs = (outputs,)

        # run backward() with only tensor that requires grad
        outputs_with_grad = []
        args_with_grad = []
        for i in range(len(outputs)):
            if torch.is_tensor(outputs[i]) and outputs[i].requires_grad:
                outputs_with_grad.append(outputs[i])
                args_with_grad.append(args[i])
        if len(outputs_with_grad) == 0:
            raise RuntimeError(
                "none of output has requires_grad=True,"
                " this checkpoint() is not necessary")
        torch.autograd.backward(outputs_with_grad, args_with_grad)
        grads = tuple(inp.grad if isinstance(inp, torch.Tensor) else None
                      for inp in detached_inputs)

        return (None, None) + grads


def checkpoint(function, *args, use_reentrant: bool = True, **kwargs):
    r"""Checkpoint a model or part of the model

    Checkpointing works by trading compute for memory. Rather than storing all
    intermediate activations of the entire computation graph for computing
    backward, the checkpointed part does **not** save intermediate activations,
    and instead recomputes them in backward pass. It can be applied on any part
    of a model.

    Specifically, in the forward pass, :attr:`function` will run in
    :func:`torch.no_grad` manner, i.e., not storing the intermediate
    activations. Instead, the forward pass saves the inputs tuple and the
    :attr:`function` parameter. In the backwards pass, the saved inputs and
    :attr:`function` is retrieved, and the forward pass is computed on
    :attr:`function` again, now tracking the intermediate activations, and then
    the gradients are calculated using these activation values.

    The output of :attr:`function` can contain non-Tensor values and gradient
    recording is only performed for the Tensor values. Note that if the output
    consists of nested structures (ex: custom objects, lists, dicts etc.)
    consisting of Tensors, these Tensors nested in custom structures will not
    be considered as part of autograd.


    .. warning::
        If :attr:`function` invocation during backward does anything different
        than the one during forward, e.g., due to some global variable, the
        checkpointed version won't be equivalent, and unfortunately it can't be
        detected.

    .. warning::
        If ``use_reentrant=True`` is specified, then if the checkpointed segment
        contains tensors detached from the computational graph by `detach()` or
        `torch.no_grad()`, the backward pass will raise an error. This is
        because `checkpoint` makes all the outputs require gradients which
        causes issues when a tensor is defined to have no gradient in the model.
        To circumvent this, detach the tensors outside of the `checkpoint`
        function. Note that the checkpointed segment can contain tensors
        detached from the computational graph if ``use_reentrant=False`` is
        specified.

    .. warning::
        If ``use_reentrant=True`` is specified, at least one of the inputs needs
        to have :code:`requires_grad=True` if grads are needed for model inputs,
        otherwise the checkpointed part of the model won't have gradients. At
        least one of the outputs needs to have :code:`requires_grad=True` as
        well. Note that this does not apply if ``use_reentrant=False`` is
        specified.

    .. warning::
        If ``use_reentrant=True`` is specified, checkpointing currently only
        supports :func:`torch.autograd.backward` and only if its `inputs`
        argument is not passed. :func:`torch.autograd.grad`
        is not supported. If ``use_reentrant=False`` is specified, checkpointing
        will work with :func:`torch.autograd.grad`.

    Args:
        function: describes what to run in the forward pass of the model or
            part of the model. It should also know how to handle the inputs
            passed as the tuple. For example, in LSTM, if user passes
            ``(activation, hidden)``, :attr:`function` should correctly use the
            first input as ``activation`` and the second input as ``hidden``
        preserve_rng_state(bool, optional):  Omit stashing and restoring
            the RNG state during each checkpoint.
            Default: ``True``
        use_reentrant(bool, optional): Use checkpointing
            implementation that requires re-entrant autograd.
            If ``use_reentrant=False`` is specified, ``checkpoint`` will use an
            implementation that does not require re-entrant autograd. This
            allows ``checkpoint`` to support additional functionality, such as
            working as expected with ``torch.autograd.grad`` and support for
            keyword arguments input into the checkpointed function. Note that future
            versions of PyTorch will default to ``use_reentrant=False``.
            Default: ``True``
        args: tuple containing inputs to the :attr:`function`

    Returns:
        Output of running :attr:`function` on :attr:`*args`
    """
    # Hack to mix *args with **kwargs in a python 2.7-compliant way
    preserve = kwargs.pop('preserve_rng_state', True)
    if kwargs and use_reentrant:
        raise ValueError("Unexpected keyword arguments: " + ",".join(arg for arg in kwargs))

    if use_reentrant:
        return CheckpointFunction.apply(function, preserve, *args)
    else:
        return _checkpoint_without_reentrant(
            function,
            preserve,
            *args,
            **kwargs,
        )


def checkpoint_sequential(functions, segments, input, use_reentrant=True, **kwargs):
    r"""A helper function for checkpointing sequential models.

    Sequential models execute a list of modules/functions in order
    (sequentially). Therefore, we can divide such a model in various segments
    and checkpoint each segment. All segments except the last will run in
    :func:`torch.no_grad` manner, i.e., not storing the intermediate
    activations. The inputs of each checkpointed segment will be saved for
    re-running the segment in the backward pass.

    See :func:`~torch.utils.checkpoint.checkpoint` on how checkpointing works.

    .. warning::
        Checkpointing currently only supports :func:`torch.autograd.backward`
        and only if its `inputs` argument is not passed. :func:`torch.autograd.grad`
        is not supported.

    .. warning:
        At least one of the inputs needs to have :code:`requires_grad=True` if
        grads are needed for model inputs, otherwise the checkpointed part of the
        model won't have gradients.

    .. warning:
        Since PyTorch 1.4, it allows only one Tensor as the input and
        intermediate outputs, just like :class:`torch.nn.Sequential`.

    Args:
        functions: A :class:`torch.nn.Sequential` or the list of modules or
            functions (comprising the model) to run sequentially.
        segments: Number of chunks to create in the model
        input: A Tensor that is input to :attr:`functions`
        preserve_rng_state(bool, optional):  Omit stashing and restoring
            the RNG state during each checkpoint.
            Default: ``True``
        use_reentrant(bool, optional): Use checkpointing
            implementation that requires re-entrant autograd.
            If ``use_reentrant=False`` is specified, ``checkpoint`` will use an
            implementation that does not require re-entrant autograd. This
            allows ``checkpoint`` to support additional functionality, such as
            working as expected with ``torch.autograd.grad`` and support for
            keyword arguments input into the checkpointed function.
            Default: ``True``

    Returns:
        Output of running :attr:`functions` sequentially on :attr:`*inputs`

    Example:
        >>> # xdoctest: +SKIP("stub")
        >>> model = nn.Sequential(...)
        >>> input_var = checkpoint_sequential(model, chunks, input_var)
    """
    # Hack for keyword-only parameter in a python 2.7-compliant way
    preserve = kwargs.pop('preserve_rng_state', True)
    if kwargs:
        raise ValueError("Unexpected keyword arguments: " + ",".join(arg for arg in kwargs))

    def run_function(start, end, functions):
        def forward(input):
            for j in range(start, end + 1):
                input = functions[j](input)
            return input
        return forward

    if isinstance(functions, torch.nn.Sequential):
        functions = list(functions.children())

    segment_size = len(functions) // segments
    # the last chunk has to be non-volatile
    end = -1
    for start in range(0, segment_size * (segments - 1), segment_size):
        end = start + segment_size - 1
        input = checkpoint(
            run_function(start, end, functions),
            input,
            use_reentrant=use_reentrant,
            preserve_rng_state=preserve
        )
    return run_function(end + 1, len(functions) - 1, functions)(input)

# NOTE [ Nestable Checkpoint ]
#
# The semantics of nested checkpoint can be defined by two basic rules.
# Following the two rules leads to an important implication that is central
# to motivating the design.
#
# Rule 1. Saved tensors are managed by inner-most checkpoint only and hidden
#         from any outer layers of checkpoint.
#
# Rule 2. The inputs of inner checkpoints are treated as tensors saved to its
#         parent checkpoint.
#
# Implication: To recompute any given saved tensor, we need to recompute all of
#              the checkpoints wrapping it.
#
# Why is this implied? To unpack a saved tensor X during backward we need to
# recompute the inner-most checkpoint (#1), and in order to recompute that
# checkpoint I need to have its inputs, which are managed by that checkpoint's
# parent (#2), which thus also needs to be recomputed first. Continue this line
# of reasoning and we realize that in order to unpack X, all checkpoints that
# were active at the time X was saved need to be recomputed. (unless we have
# already done so in that backward for some other saved tensor).
#
# In practice, we record a stack of _CheckpointFrame mirroring the levels of
# nesting, where each _CheckpointFrame stores the information necessary to
# recompute a particular checkpoint. Everytime a tensor is saved, the current
# state of the stack is snapshotted, and when that tensor needs to be unpacked,
# each of the snapshotted checkpoints is recomputed in sequence from outer-most
# to inner-most. We call the stack of _CheckpointFrame the _CheckpointStack.
#
# [ Implementation details of Rule 2 ]
#
# Rule 2 specifies that we should store the inputs of inner checkpoints onto the
# parent, but this is not possible if the checkpoint is already the outer-most.
# In practice, we consider two cases:
#
#  1) If checkpoint does not have a parent, we store them directly on the
#     checkpoint frame.
#  2) If checkpoint has a parent, we rely on a noop autograd Function whose only
#     job is to save its inputs as saved tensors.
#
# We must also have additional bookkeeping logic distinguishing inputs because
# inputs differ from normal saved tensors in that we don't unpack before using
# them. This means that (1) we cannot rely on the packed handle to identify them
# (2) we should not detach them, because we cannot rely on autograd to smash
# the autograd meta back.
#
# Rule 3. We should start recomputation as if there are no checkpoints currently
#         active. Checkpoints encountered during recomputation are still
#         respected.
#
# To motivate Rule 3, consider the example (this case and others is covered in
# more detail Rule 6):
#
# def g(x):
#    out = x.sin().exp()
#    gx, torch.autograd.grad(out, x, create_graph=True)  # (1)
#    return gx
#
# def h(x):
#    out = checkpoint1(g)
#    out.backward()                                      # (2)
#
# checkpoint2(h)(x)
#
# A Problem is Encountered:
#
# When the outer checkpoint1 is being recomputed at (2), checkpoint2 is still
# active. If we naively follow Rule 1, when things are saved when recomputing
# g, checkpoint2 should manage our saved tensors, since it is the inner-most.
# That is obviously not desired, since it would mean at (1), we'd need to
# recompute h again, which we were trying to do in the first place. This is
# an infinite loop!
#
# Rule 3 saves us in this scenario. If we begin g's recompute with no
# checkpoints active, we can proceed with (1) without having to recompute h.
#
# [ Implementation details of Rule 3 ]
#
# To implement Rule 3, we want to do two things:
#
# 1) We must somehow stash the current stack as we begin recomputation and then
#    restore that stack after recomputation completes.
# 2) Upon recomputation, we should have our own new stack to facilitate the
#    handling of any checkpointing encountered there.
#
# The natural way to model this is necessarily with another stack.
#
# What we have is a stack of _CheckpointStack, each of which holds a stack
# of _CheckpointFrame. We push a _CheckpointStack everytime we begin
# recomputation and pop a _CheckpointStack upon completion. An empty stack means
# that no checkpoints are active.
#
# (Notably, the pushing and popping of _CheckpointStack also coincides with the
# the pushing and popping of SavedTensorHooks. The hooks meant for recomputation
# are only active when the current stack is empty)
#
#                                * PART 2 *
#
# Beyond the basic semantics specific to nested checkpoint, we impose several
# more constraints that may apply to checkpointing in general.
#
# Rule 4. Lifetime of recomputed tensors
#
#         Recomputed tensors are considered specific to particular invocations
#         of backward and are always cleared immediately as they are unpacked
#         Particularly, we require this to happen even if retain_graph=True.
#
# [ Implementation details of Rule 4 ]
#
# If we were okay with recomputed tensors staying alive after backward is run
# with retain_graph=True, we would store recomputed variables as the values of a
# WeakKeyDictionary and pack strong references to the keys, so that as we
# backward, those packed keys would be cleared as long as retain_graph=False.
# Clearing the packed key clears the corresonding entry in the WKD.
#
# If we wish recomputed variables to be immediately cleared as we unpack them in
# the retain_graph=True case, we cannot rely on the packed keys to be cleared by
# backward automatically. Instead of packing the strong reference to the key
# directly, we pack a container object, which we manually clear as we unpack.
#
# An important detail is that if a second backward happens, the second
# recomputation needs to reset the container with a newly created key.
#
# Rule 5. Stop recomputation as soon as we've recomputed the saved tensors we
#         know we need.
#
# [ Implementation details of Rule 5 ]
#
# During recomputation, raise an exception if the number of recomputed tensors
# matches the number of tensors that we expected to recompute. We wrap the
# recomputation call with a try-catch to catch this specific exception. See
# Rule #5 below for some examples.
#
# Rule 6. We support doing backward inside checkpoint context
#
# This section is just a bunch of random examples that we'd like to support,
# and comments on how that forced us to make certain design decisions.
#
# [ Basic case ]
#
# def fn(x):
#   y = x.sin()
#   z = y.cos()
#   gx, = torch.autograd.grad(z, x, retains_grad=True)
#   return gx, z
#
# out = checkpoint(fn)(inp)
#
# Because z is saved by cos while checkpoint is enabled, it would not be
# actually saved, and so the .grad() call inside must trigger a recomputation.
#
# During recomputation the "inner pack hook" has two responsibilities:
#
# 1) As usual, populating the WeakKeyDictionary storing recomputed tensors
# 2) Pack the tensor as-is so that one may perform backward on the recomputed
#    graph. The tensors saved to this graph will live until the end of
#    recomputation, or earlier if someone performs backward with
#    retain_graph=False or something.
#
# [ Multiple backwards ]
#
# The example below shows what happens if during recomputation we find that some
# of the tensors we are trying to recompute have already been cleared.
#
# Spoiler: we don't do anything special, we just skip over them!
#
# def fn(x):
#   y = x.sin()                           # (1)
#   z = y.cos()                           # (2)
#   gx, = torch.autograd.grad(z, x)       # (3)
#   w = x.sin()                           # (4)
#   v = w.cos()                           # (5)
#   gx2, = torch.autograd.grad(v, x)      # (6)
#   return x * gx * gx2
#
# out = checkpoint(fn)(inp)
#
# In the code above fn is computed 4 times in total.
#   1. Don't save x and y since we are inside a checkpoint.
#   2. Trigger a recompute of fn as we reach (3) since x and y weren't saved.
#   3. If early stop is enabled, stop at (2)
#   4. Continue original forward at (4), not saving x and w.
#   5. (5) triggers a recompute of fn
#   6. During recompute, we see that in the original graph, gx has already
#      cleared x and y since backward is run at (3) without retain_graph=True
#      We save x and w, however.
#   7. Continue with returning
#
# [ backward within nested checkpoint ]
#
# Another case to consider is when we do backward within checkpoint, but we are
# also in a nested checkpoint. Properly handling this case has implications for
# how inputs are saved.
#
# def f(x):
#   y = x.sin()
#   z = y.cos()
#   gx, = torch.autograd.grad(z, x)       # (1)
#   return z
#
# def g(x):
#   return checkpoint(f)(x)
#
# out = checkpoint(g)(inp)
#
# In the above example, when we recompute for the outer checkpoint (the one
# wrapping g), we are recomputing checkpointed f.
#
# When checkpointed f was original computed in forward, there was already a
# checkpoint active when we entered f's checkpoint, which means that f's
# checkpoint is nested. However, during the recomputation of g, we enter
# into a checkpoint wrapping f yet again, but this time there is no longer a
# checkpoint active. How should we save f's inputs in this situation?
#
# Recall that we should save f's inputs onto the parent checkpoint if we are
# nested, or directly onto the frame otherwise, so on the surface it sounds like
# we should save directly onto the frame.
#
# Strangely, the answer here is actually both!
#
# In addition to saving our inputs onto the frame directly, we also save f's
# inputs onto target frame. We do this for two reasons:
#
# 1) First, if we did not save f's inputs directly onto its own frame we would
#    not be able to recompute f during recomputation for - see line marked (1)
#    above in the example.
# 2) Second, since f's checkpoint was originally saved its inputs as if it were
#    nested, it must also save its inputs as if it were nested so that the
#    indices of the recomputed variables match.
#

# NB: This is temporary and should be removed in a follow up PR. Early stopping
#     is currently disabled by default. Since some nested test cases require
#     ealry stopping to pass, _set_checkpoint_early_stop can be used to enable.
_enable_checkpoint_early_stop = False

@contextlib.contextmanager
def _set_checkpoint_early_stop(enable):
    global _enable_checkpoint_early_stop
    try:
        prev = _enable_checkpoint_early_stop
        _enable_checkpoint_early_stop = enable
        yield
    finally:
        _enable_checkpoint_early_stop = prev

# See NOTE [ Nestable Checkpoint ] Rule #4
class _Handle():
    pass

class _Holder():
    def __init__(self, handle: _Handle):
        self.handle: _Handle = handle

# Reimplementation of torch.distributed.utils.{_pack,_unpack}_kwargs to avoid a import cycle
def _pack_kwargs(*args: Any, **kwargs: Any) -> Tuple[Tuple[Any, ...], Tuple[str, ...]]:
    kwarg_keys: List[str] = []
    flat_args: List[Any] = list(args)
    for k, v in kwargs.items():
        kwarg_keys.append(k)
        flat_args.append(v)

    return tuple(flat_args), tuple(kwarg_keys)

def _unpack_kwargs(flat_args: Tuple[Any, ...], kwarg_keys: Tuple[str, ...]) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    assert len(kwarg_keys) <= len(flat_args), f"too many keys {len(kwarg_keys)} vs. {len(flat_args)}"
    if len(kwarg_keys) == 0:
        return flat_args, {}
    args = flat_args[: -len(kwarg_keys)]
    kwargs = {k: v for k, v in zip(kwarg_keys, flat_args[-len(kwarg_keys) :])}
    return args, kwargs

class _NoopSaveInputs(torch.autograd.Function):
    # Autograd Function that saves inputs and returns them as-is
    # This is not used directly, see _applyAutogradFunctionToSaveInputs below.
    @staticmethod
    def forward(*args):
        return args

    @staticmethod
    def setup_context(ctx: Any, inputs: Tuple[Any, ...], output: Any) -> None:
        ctx.save_for_backward(*inputs)

    @staticmethod
    def backward(ctx, *grad_outputs):
        return grad_outputs

def _applyAutogradFunctionToSaveInputs(*args: torch.Tensor) -> Tuple[torch.Tensor, ...]:
    # Wrapper around _NoopSaveInputs to preserve the requires_grad-ness of the inputs
    idx_no_req_grad = [i for i, t in enumerate(args) if isinstance(t, torch.Tensor)
                       and not t.requires_grad and (t.is_floating_point() or t.is_complex())]
    new_args = _NoopSaveInputs.apply(*args)
    return tuple(t.detach() if i in idx_no_req_grad else t for i, t in enumerate(new_args))

class _CheckpointFrame():
    def __init__(self, recompute_fn):
        self.recompute_fn = recompute_fn

        # See [ Implementation details of Rule 2 ]
        self.maybe_args: Optional[Tuple[torch.Tensor, ...]] = None
        self.args_idx: Optional[List[int]] = None
        self.child_args_idx : List[int] = []
        self.args_handles: Optional[List[Any]] = None

        self.weak_holders: List[ReferenceType] = []

        self.recomputed: DefaultDict[int, weakref.WeakKeyDictionary[_Handle, torch.Tensor]] = \
            defaultdict(weakref.WeakKeyDictionary)
        self.recomp_counter: DefaultDict[int, int] = defaultdict(lambda: 0)
        self.is_recomputed: DefaultDict[int, bool] = defaultdict(lambda: False)

    def __repr__(self):
        return f"Frame({id(self)})"

    def get_args_from_parent(self, parent_frame, gid):
        assert self.args_idx is not None
        out = []
        for idx in self.args_idx:
            holder = parent_frame.weak_holders[idx]()
            assert holder is not None
            out.append(parent_frame.recomputed[gid][holder.handle])
        return out

    def save_args_to_parent(self, parent_frame, args):
        # Some bookkeeping to remember where the child args are saved on the parent
        parent_pre_len = len(parent_frame.weak_holders)
        new_args = _applyAutogradFunctionToSaveInputs(*args)
        self.args_handles = parent_frame.weak_holders[parent_pre_len: len(parent_frame.weak_holders)]
        indices = list(range(parent_pre_len, len(parent_frame.weak_holders)))
        parent_frame.child_args_idx.extend(indices)
        self.args_idx = indices
        return new_args

class _CheckpointStack(NamedTuple):
    stack: List[_CheckpointFrame]
    is_recompute: bool

# This stack is synced with the saved_tensor_hook stack.
# When _recomputation_hook is pushed onto the hook stack, we also push a new
# empty _CheckpointStack
_checkpoint_stacks: List[_CheckpointStack] = \
    [_CheckpointStack(stack=[], is_recompute=False)]

def _reset_checkpoint_stacks():
    # This can be helpful for testing. When a test fails, this global state is
    # likely corrupted and needs to be reset.
    global _checkpoint_stacks
    _checkpoint_stacks = [_CheckpointStack(stack=[], is_recompute=False)]

# See Rule 5
class _StopRecomputationError(Exception):
    pass

class _recomputation_hook(torch.autograd.graph.saved_tensors_hooks):
    def __init__(self, target_frame_ref: ReferenceType, gid: int):
        def pack_hook(x):
            target_frame = target_frame_ref()
            assert target_frame is not None
            recomp_idx = target_frame.recomp_counter[gid]
            target_frame.recomp_counter[gid] += 1
            holder = target_frame.weak_holders[recomp_idx]()

            if holder is not None:
                # See Rule 6: [ Multiple backwards ] above
                if holder.handle is None:
                    # See Rule 4 above
                    holder.handle = _Handle()
                target_frame.recomputed[gid][holder.handle] = \
                    x if recomp_idx in target_frame.child_args_idx else x.detach()

            # TODO: figure out why some tests are failing when early stop is not enabled
            if _enable_checkpoint_early_stop and \
               target_frame.recomp_counter[gid] == len(target_frame.weak_holders):
                raise _StopRecomputationError()
            # See Rule 6: [ Basic case ] above
            return x.detach()

        def unpack_hook(x):
            return x

        super().__init__(pack_hook, unpack_hook)

class _checkpoint_hook(torch.autograd.graph.saved_tensors_hooks):
    def __init__(self):
        def pack_hook(_unused_x):
            # Snapshot the state of the current checkpoint stack
            current_frames, is_recompute = _checkpoint_stacks[-1]
            top_frame = current_frames[-1]
            # See Rule 4 above
            handle = _Handle()
            holder = _Holder(handle)
            top_frame.weak_holders.append(weakref.ref(holder))
            return holder, tuple(current_frames)

        def unpack_hook(saved):
            holder, frames = saved

            top_frame = frames[-1]
            gid = torch._C._current_graph_task_id()
            if gid == -1:
                # generate a temporary id if we trigger unpack outside of a backward call
                gid = int(uuid.uuid4())

            for i in range(len(frames)):
                frame = frames[i]
                if frame.is_recomputed[gid]:
                    continue
                # See [ Implementation details of Rule 2 ]
                if frame.maybe_args is None:
                    args = frame.get_args_from_parent(frames[i - 1], gid)
                else:
                    args = frame.maybe_args
                try:
                    _checkpoint_stacks.append(_CheckpointStack(stack=[], is_recompute=True))
                    # pass gid in in case we do reentrant backward
                    with _recomputation_hook(weakref.ref(frame), gid), torch.autograd.enable_grad():
                        frame.recompute_fn(*args)
                        assert not _enable_checkpoint_early_stop, \
                            "if early stop is enabled, we don't expect to reach here"
                except _StopRecomputationError as e:
                    _checkpoint_stacks.pop()
                    pass
                frame.is_recomputed[gid] = True

            if holder.handle is None:
                raise RuntimeError(
                    "If you are calling ctx.saved_tensor in backward, make sure to do so only once. "
                    "Otherwise please open an issue with details on your use case."
                )
            if holder.handle not in top_frame.recomputed[gid]:
                raise RuntimeError(
                    "Attempt to retrieve a tensor saved by autograd multiple times without checkpoint"
                    " recomputation being triggered in between, this is not currently supported. Please"
                    " open an issue with details on your use case."
                )

            ret = top_frame.recomputed[gid][holder.handle]
            holder.handle = None
            return ret

        super().__init__(pack_hook, unpack_hook)

# NB: this helper wraps fn before calling checkpoint_impl. kwargs and
#     saving/restoring of global state is handled here.
def _checkpoint_without_reentrant(fn, preserve_rng_state=True, *args, **kwargs):
    """Checkpointining without re-entrant autograd
    Args:
        function: describes what to run in the forward pass of the model or
            part of the model. It should also know how to handle the inputs
            passed as the tuple. For example, in LSTM, if user passes
            ``(activation, hidden)``, :attr:`function` should correctly use the
            first input as ``activation`` and the second input as ``hidden``
        preserve_rng_state(bool, optional):  Omit stashing and restoring
            the RNG state during each checkpoint.
            Default: ``True``
        *args: Arguments to pass in to the given ``function``.
        **kwargs: Keyword arguments to pass into the given ``function``.
    """
    # Accommodates the (remote) possibility that autocast is enabled for cpu AND gpu.
    gpu_autocast_kwargs, cpu_autocast_kwargs = _get_autocast_kwargs()

    if preserve_rng_state:
        fwd_cpu_state = torch.get_rng_state()
        # Don't eagerly initialize the cuda context by accident.
        # (If the user intends that the context is initialized later, within their
        # run_function, we SHOULD actually stash the cuda state here.  Unfortunately,
        # we have no way to anticipate this will happen before we run the function.
        # If they do so, we raise an error.)
        had_cuda_in_fwd = False
        if torch.cuda._initialized:
            had_cuda_in_fwd = True
            fwd_gpu_devices, fwd_gpu_states = get_device_states(*args)

    # From checkpoint_wrapper.
    # We should modify to handle non-tensor, kwargs
    flat_args, kwarg_keys = _pack_kwargs(*args, **kwargs)

    def new_fn(*inputs):
        # This function should be called immediately by checkpoint_impl
        unpacked_args, unpacked_kwargs = _unpack_kwargs(
            inputs, kwarg_keys
        )
        out = fn(*unpacked_args, **unpacked_kwargs)
        if torch.cuda._initialized and preserve_rng_state and not had_cuda_in_fwd:
            # Cuda was not initialized before running the forward, so we didn't
            # stash the CUDA state.
            raise RuntimeError(
                "PyTorch's CUDA state was initialized in the forward pass "
                "of a Checkpoint, which is not allowed. Please open an issue "
                "if you need this feature.")
        return out

    def recompute_fn(*inputs):
        # This will be called later during recomputation. This wrapping enables
        # the necessary global state to be captured.
        unpacked_args, unpacked_kwargs = _unpack_kwargs(
            inputs, kwarg_keys
        )

        rng_devices = []
        if preserve_rng_state and had_cuda_in_fwd:
            rng_devices = fwd_gpu_devices
        with torch.random.fork_rng(devices=rng_devices, enabled=preserve_rng_state):
            if preserve_rng_state:
                torch.set_rng_state(fwd_cpu_state)
                if had_cuda_in_fwd:
                    set_device_states(fwd_gpu_devices, fwd_gpu_states)

            with torch.cuda.amp.autocast(**gpu_autocast_kwargs), \
                 torch.cpu.amp.autocast(**cpu_autocast_kwargs):
                fn(*unpacked_args, **unpacked_kwargs)

    return _checkpoint_impl(new_fn, recompute_fn, *flat_args)

def _checkpoint_impl(fn, recompute_fn, *args):
    curr_stack, is_curr_stack_recompute = _checkpoint_stacks[-1]
    new_frame = _CheckpointFrame(recompute_fn)

    # See [ Implementation details of Rule 2 ]
    if len(curr_stack) > 0:
        args = new_frame.save_args_to_parent(curr_stack[-1], args)
    elif is_curr_stack_recompute:
        # See Rule 6 [ backward within nested checkpoint ] above
        args = _applyAutogradFunctionToSaveInputs(*args)
        new_frame.maybe_args = args
    else:
        new_frame.maybe_args = args

    curr_stack.append(new_frame)
    with _checkpoint_hook():
        ret = fn(*args)
    curr_stack.pop()
    return ret
