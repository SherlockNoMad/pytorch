//  Copyright © 2022 Apple Inc.

#include <ATen/ATen.h>
#include <ATen/Tensor.h>
#include <ATen/Utils.h>
#include <ATen/mps/MPSStream.h>
#include <ATen/native/mps/Copy.h>
#include <ATen/native/mps/OperationUtils.h>
#include <torch/library.h>

namespace at {
namespace native {
namespace mps {

typedef MPSGraphTensor* (^UnaryOpBlock)(MPSGraph*, MPSGraphTensor*);
#define ConditionalOpFn(void) NSArray<MPSGraphTensor *> * (void)

void unary_op(const Tensor& self, const Tensor& output, std::string op_name, UnaryOpBlock unaryBlock)
{
  TORCH_CHECK_TYPE(self.scalar_type() != ScalarType::Long, "Operation '", op_name, "()' does not support input type 'int64' in MPS backend.");
  if (!output.is_same_size(self)) {
    output.resize_(self.sizes());
  }
  // Empty tensor is noop
  if (self.numel() == 0) {
    return;
  }
  MPSGraphCache* cache_ = MPSGraphCache::getInstance();
  @autoreleasepool {
    string key = op_name + getTensorsStringKey({self}, /*use_scalar_value*/ false);
    auto cachedGraph = cache_->LookUpAs<MPSUnaryCachedGraph>(key);

    if(!cachedGraph) {
      MPSCachedGraph *tmpCachedGraph = cache_->CreateCachedGraph(key, ^ MPSCachedGraph* () {
        MPSUnaryCachedGraph *newCachedGraph = nil;
        @autoreleasepool {
          MPSGraph* mpsGraph = make_mps_graph();
          newCachedGraph = new MPSUnaryCachedGraph(mpsGraph);
          newCachedGraph->inputTensor_ = mpsGraphRankedPlaceHolder(mpsGraph, self);
          MPSGraphTensor* castTensor = newCachedGraph->inputTensor_;
          // Integer input must be cast to float if output is float
          if (isIntegralType(self.scalar_type()) && isFloatingType(output.scalar_type())) {
            castTensor = castMPSTensor(mpsGraph, newCachedGraph->inputTensor_, output.scalar_type());
          }
          newCachedGraph->outputTensor_ = unaryBlock(mpsGraph, castTensor);
        }
        return newCachedGraph;
      });
      cachedGraph = tmpCachedGraph->as<MPSUnaryCachedGraph>();
    }

    Placeholder selfPlaceholder = Placeholder(cachedGraph->inputTensor_, self);
    Placeholder outputPlaceholder = Placeholder(cachedGraph->outputTensor_, output);
    NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* feeds = @{
      selfPlaceholder.getMPSGraphTensor() : selfPlaceholder.getMPSGraphTensorData()
    };
    NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* results = @{
      outputPlaceholder.getMPSGraphTensor() : outputPlaceholder.getMPSGraphTensorData()
    };
    runMPSGraph(getCurrentMPSStream(), cachedGraph->graph(), feeds, results);
  }
}

MPSGraphTensor* trunc_tensor(MPSGraph* mpsGraph, MPSGraphTensor* inputTensor)
{
  MPSGraphTensor* zeroTensor = [mpsGraph constantWithScalar:0.0
                                                   dataType:inputTensor.dataType];
  MPSGraphTensor* predicateTensor = [mpsGraph lessThanWithPrimaryTensor:inputTensor
                                                        secondaryTensor:zeroTensor
                                                                    name:nil];
  return [mpsGraph selectWithPredicateTensor:predicateTensor
                         truePredicateTensor:[mpsGraph ceilWithTensor :inputTensor name:nil]
                        falsePredicateTensor:[mpsGraph floorWithTensor:inputTensor name:nil]
                                        name:nil];
};

} // namespace mps

TORCH_IMPL_FUNC(trunc_out_mps) (const Tensor& self, const Tensor& output) {
  mps::unary_op(self, output, "trunc_out_mps",
                ^ MPSGraphTensor* (MPSGraph* mpsGraph, MPSGraphTensor* inputTensor)
                  { return mps::trunc_tensor(mpsGraph, inputTensor); });
}

#define CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(func_out, func_stub)              \
TORCH_IMPL_FUNC(func_out) (const Tensor& self, const Tensor& output) {                \
  mps::unary_op(self, output, #func_out,                                              \
                ^ MPSGraphTensor* (MPSGraph* mpsGraph, MPSGraphTensor* inputTensor)   \
                  { return [mpsGraph func_stub##WithTensor:inputTensor name:nil]; }); \
}

#define CREATE_MPS_UNARY_TORCH_IMPL_FUNC(func_out, func_stub)                         \
Tensor& func_out(const Tensor& self, Tensor& output) {                                \
  mps::unary_op(self, output, #func_out,                                              \
                ^ MPSGraphTensor* (MPSGraph* mpsGraph, MPSGraphTensor* inputTensor)   \
                  { return [mpsGraph func_stub##WithTensor:inputTensor name:nil]; }); \
  return output;                                                                      \
}


CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(exp_out_mps, exponent)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(exp2_out_mps, exponentBase2)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(reciprocal_out_mps, reciprocal)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(sqrt_out_mps, squareRoot)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(rsqrt_out_mps, reverseSquareRoot)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(sign_out_mps, sign)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(neg_out_mps, negative)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(log_out_mps, logarithm)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(log10_out_mps, logarithmBase10)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(log2_out_mps, logarithmBase2)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(ceil_out_mps, ceil)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(floor_out_mps, floor)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(round_out_mps, round)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(erf_out_mps, erf)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(sin_out_mps, sin)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(cos_out_mps, cos)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(tan_out_mps, tan)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(asin_out_mps, asin)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(acos_out_mps, acos)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(atan_out_mps, atan)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(sinh_out_mps, sinh)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(cosh_out_mps, cosh)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(tanh_out_mps, tanh)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(asinh_out_mps, asinh)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(acosh_out_mps, acosh)
CREATE_MPS_STRUCTURED_UNARY_TORCH_IMPL_FUNC(atanh_out_mps, atanh)

CREATE_MPS_UNARY_TORCH_IMPL_FUNC(abs_out_mps, absolute)

Tensor& logical_not_out_mps(const Tensor& self, Tensor& output)
{
  auto bool_self = self.to(ScalarType::Bool);
  mps::unary_op(bool_self, output, "logical_not_out_mps", [](MPSGraph* mpsGraph, MPSGraphTensor* inputTensor){ return [mpsGraph notWithTensor:inputTensor name:nil];});
  return output;
}

TORCH_IMPL_FUNC(log1p_out_mps) (const Tensor& self, const Tensor& output)
{
    using namespace mps;
    if (!output.is_same_size(self)) {
      output.resize_(self.sizes());
    }
    MPSGraphCache* cache_ = MPSGraphCache::getInstance();
    @autoreleasepool {
      string key = string("log1p_out_mps") + getTensorsStringKey({self});
      auto cachedGraph = cache_->LookUpAs<MPSUnaryCachedGraph>(key);

      if(!cachedGraph) {
        MPSCachedGraph *tmpCachedGraph = cache_->CreateCachedGraph(key, ^ MPSCachedGraph* () {
          MPSUnaryCachedGraph *newCachedGraph = nil;
          @autoreleasepool {
            MPSGraph* mpsGraph = make_mps_graph();
            newCachedGraph = new MPSUnaryCachedGraph(mpsGraph);
            newCachedGraph->inputTensor_ = mpsGraphRankedPlaceHolder(mpsGraph, self);
              MPSGraphTensor* oneTensor = [mpsGraph constantWithScalar:1.0
                                                          shape:getMPSShape(self)
                                                       dataType:mps::getMPSDataType(self.scalar_type())];
              MPSGraphTensor* addedTensor = [mpsGraph additionWithPrimaryTensor:newCachedGraph->inputTensor_
                                                         secondaryTensor:oneTensor
                                                                    name:nil];
            newCachedGraph->outputTensor_ = [mpsGraph logarithmWithTensor:addedTensor
                                                                    name:nil];
          }
          return newCachedGraph;
        });
        cachedGraph = tmpCachedGraph->as<MPSUnaryCachedGraph>();
      }

      Placeholder selfPlaceholder = Placeholder(cachedGraph->inputTensor_, self);
      Placeholder outputPlaceholder = Placeholder(cachedGraph->outputTensor_, output);
      NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* feeds = @{
        selfPlaceholder.getMPSGraphTensor() : selfPlaceholder.getMPSGraphTensorData()
      };
      NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* results = @{
        outputPlaceholder.getMPSGraphTensor() : outputPlaceholder.getMPSGraphTensorData()
      };
      runMPSGraph(getCurrentMPSStream(), cachedGraph->graph(), feeds, results);
    }
}

TORCH_IMPL_FUNC(sgn_out_mps) (const Tensor& self, const Tensor& output)
{
    using namespace mps;

    if (!output.is_same_size(self)) {
      output.resize_(self.sizes());
    }

    string graphSuffix = "_real";
    Tensor realInput;
    Tensor realOutput;
    Tensor flatInput = self.flatten();
    Tensor flatOutput = output.flatten();
    if (self.is_complex()) {
      realInput = at::view_as_real(flatInput);
      realOutput = at::view_as_real(flatOutput);
      graphSuffix = "_complex";
    } else {
      realInput = flatInput;
      realOutput = flatOutput;
    }

    MPSGraphCache* cache_ = MPSGraphCache::getInstance();
    @autoreleasepool {
      string key = string("sgn_out_mps") + getTensorsStringKey({realInput}) + graphSuffix;
      auto cachedGraph = cache_->LookUpAs<MPSUnaryCachedGraph>(key);

      if(!cachedGraph) {
        MPSCachedGraph *tmpCachedGraph = cache_->CreateCachedGraph(key, ^ MPSCachedGraph* () {
          MPSUnaryCachedGraph *newCachedGraph = nil;
          @autoreleasepool {
            MPSGraph* mpsGraph = make_mps_graph();
            newCachedGraph = new MPSUnaryCachedGraph(mpsGraph);
            newCachedGraph->inputTensor_ = mpsGraphRankedPlaceHolder(mpsGraph, realInput);
              MPSGraphTensor* sgnTensor;
              if (self.is_complex()) {
                NSArray<MPSGraphTensor*>* complexNumberComponents = [mpsGraph splitTensor:newCachedGraph->inputTensor_
                                                              numSplits: 2
                                                              axis: 1
                                                              name: nil];

                MPSGraphTensor* realPartTensor = complexNumberComponents[0];
                MPSGraphTensor* imaginaryPartTensor = complexNumberComponents[1];

                MPSGraphTensor* zeroTensor = [mpsGraph constantWithScalar:0.0
                                                              shape:realPartTensor.shape
                                                              dataType:realPartTensor.dataType];

                MPSGraphTensor* complexZeroTensor = [mpsGraph constantWithScalar:0.0
                                                              shape: newCachedGraph->inputTensor_.shape
                                                              dataType:realPartTensor.dataType];                

                MPSGraphTensor* isRealZero = [mpsGraph equalWithPrimaryTensor:realPartTensor
                                                              secondaryTensor:zeroTensor
                                                              name: nil];

                MPSGraphTensor* isImaginaryZero = [mpsGraph equalWithPrimaryTensor:imaginaryPartTensor
                                                              secondaryTensor:zeroTensor
                                                              name: nil];

                MPSGraphTensor* isComplexZero = [mpsGraph logicalANDWithPrimaryTensor:isRealZero
                                                              secondaryTensor:isImaginaryZero
                                                              name: nil];

                MPSGraphTensor* sgnDenomReal = [mpsGraph squareWithTensor:realPartTensor
                                                              name: nil];

                MPSGraphTensor* sgnDenomImaginary = [mpsGraph squareWithTensor:imaginaryPartTensor
                                                              name: nil];

                MPSGraphTensor* sgnDenomSum = [mpsGraph additionWithPrimaryTensor:sgnDenomReal
                                                              secondaryTensor:sgnDenomImaginary
                                                              name: nil];

                MPSGraphTensor* sgnDenom = [mpsGraph squareRootWithTensor:sgnDenomSum
                                                              name: nil];

                MPSGraphTensor* sgnRealTensor = [mpsGraph divisionWithPrimaryTensor:realPartTensor
                                                              secondaryTensor:sgnDenom
                                                              name: nil];

                MPSGraphTensor* sgnImaginaryTensor = [mpsGraph divisionWithPrimaryTensor:imaginaryPartTensor
                                                              secondaryTensor:sgnDenom
                                                              name: nil];

                MPSGraphTensor* sgnComplexTensor = [mpsGraph concatTensors:@[sgnRealTensor, sgnImaginaryTensor]
                                                              dimension: 1
                                                              name: nil];

                sgnTensor = [mpsGraph selectWithPredicateTensor:isComplexZero
                                                              truePredicateTensor:complexZeroTensor
                                                              falsePredicateTensor:sgnComplexTensor
                                                              name:nil];
              } else {
                MPSGraphTensor* zeroTensor = [mpsGraph constantWithScalar:0
                                                              shape:newCachedGraph->inputTensor_.shape
                                                              dataType:mps::getMPSDataType(self.scalar_type())];

                MPSGraphTensor* oneTensor = [mpsGraph constantWithScalar:1
                                                              shape:newCachedGraph->inputTensor_.shape
                                                              dataType:mps::getMPSDataType(self.scalar_type())];

                MPSGraphTensor* negativeOneTensor = [mpsGraph constantWithScalar:-1
                                                              shape:newCachedGraph->inputTensor_.shape
                                                              dataType:mps::getMPSDataType(self.scalar_type())];

                MPSGraphTensor* isPositive = [mpsGraph greaterThanWithPrimaryTensor:newCachedGraph->inputTensor_
                                                              secondaryTensor:zeroTensor
                                                              name: nil];

                MPSGraphTensor* isNegative = [mpsGraph lessThanWithPrimaryTensor:newCachedGraph->inputTensor_
                                                              secondaryTensor:zeroTensor
                                                              name: nil];

                MPSGraphTensor* notPositiveTensor = [mpsGraph selectWithPredicateTensor:isNegative
                                                              truePredicateTensor:negativeOneTensor
                                                              falsePredicateTensor:zeroTensor
                                                              name:nil];

                sgnTensor = [mpsGraph selectWithPredicateTensor:isPositive
                                                              truePredicateTensor:oneTensor
                                                              falsePredicateTensor:notPositiveTensor
                                                              name:nil];
              }
              newCachedGraph->outputTensor_ = sgnTensor;
          }
          return newCachedGraph;
        });
        cachedGraph = tmpCachedGraph->as<MPSUnaryCachedGraph>();
      }

      Placeholder selfPlaceholder = Placeholder(cachedGraph->inputTensor_, realInput);
      Placeholder outputPlaceholder = Placeholder(cachedGraph->outputTensor_, realOutput);
      NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* feeds = @{
        selfPlaceholder.getMPSGraphTensor() : selfPlaceholder.getMPSGraphTensorData()
      };
      NSDictionary<MPSGraphTensor*, MPSGraphTensorData*>* results = @{
        outputPlaceholder.getMPSGraphTensor() : outputPlaceholder.getMPSGraphTensorData()
      };
      runMPSGraph(getCurrentMPSStream(), cachedGraph->graph(), feeds, results);
    }

    if (self.is_complex()) {
      std::vector<long long> realSize = self.sizes().vec();
      realSize.push_back(2);

      Tensor originalShape = realOutput.reshape(realSize);
      Tensor complexOutput = at::view_as_complex(originalShape);
      output.copy_(complexOutput);
    } else {
      Tensor originalShape = at::reshape(realOutput, self.sizes());
      output.copy_(originalShape);
    }
}

} // namespace native
} // namespace at
