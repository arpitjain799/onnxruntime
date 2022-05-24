// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include "core/framework/framework_common.h"
#include "core/framework/session_state.h"
#include "core/framework/tensorprotoutils.h"
#include "core/framework/utils.h"
#include "core/providers/cpu/tensor/utils.h"
#include "gsl/gsl"
#include "contrib_ops/cpu/transformers/subgraph_base.h"
#include "contrib_ops/cpu/transformers/dump_tensor.h"

using namespace ONNX_NAMESPACE;
using namespace onnxruntime::common;

namespace onnxruntime {
namespace contrib {
namespace transformers {

Subgraph::Subgraph(
    const onnxruntime::Node& node_in,
    const std::string& attribute_name,
    const GraphViewer& subgraph_in)
    : node(node_in), attribute(attribute_name), subgraph(subgraph_in), num_heads(0), head_size(0), vocab_size(0), num_layers(0), allocator_(nullptr), is_output_float16_(false) {
  num_implicit_inputs = static_cast<int>(node.ImplicitInputDefs().size());

  auto& subgraph_inputs = subgraph.GetInputs();
  auto& subgraph_outputs = subgraph.GetOutputs();

  // inputs: input_ids, position_ids, attention_mask, past_0, past_1, ...
  // outputs: logits, present_0, present_1, ...
  num_subgraph_inputs = static_cast<int>(subgraph_inputs.size());
  num_subgraph_outputs = static_cast<int>(subgraph_outputs.size());

  // CheckSubgraph will verify inputs and outputs later.
  subgraph_input_names.reserve(num_subgraph_inputs);
  for (int i = 0; i < num_subgraph_inputs; ++i) {
    subgraph_input_names.push_back(subgraph_inputs[i]->Name());
  }

  subgraph_output_names.reserve(num_subgraph_outputs);
  for (int i = 0; i < num_subgraph_outputs; ++i) {
    subgraph_output_names.push_back(subgraph_outputs[i]->Name());
  }
}

Status Subgraph::Setup(const SessionState& session_state,
                       const SessionState& subgraph_session_state) {
  session_state_ = &session_state;
  subgraph_session_state_ = &subgraph_session_state;

  std::vector<std::string> feed_names;
  feed_names.reserve(static_cast<size_t>(num_subgraph_inputs) + static_cast<size_t>(num_implicit_inputs));

  // Currently, input_ids is in CPU even for CUDA operator, so we have to use first output to find device location.
  const OrtMemoryInfo& default_location = utils::FindMemoryInfoForValue(subgraph_session_state, subgraph_output_names[0]);

  // position_ids, attention_mask, past_0, ... are created by this operator so the name doesn't matter.
  // as we skip them when we call FindDevicesForValues, and default them to be in the same device as input_ids
  feed_names.insert(feed_names.end(), subgraph_input_names.begin(), subgraph_input_names.end());

  for (auto& entry : node.ImplicitInputDefs()) {
    feed_names.push_back(entry->Name());
  }

  std::vector<OrtDevice> feed_locations;
  feed_locations.resize(feed_names.size());

  for (size_t i = 0, end = feed_names.size(); i < end; ++i) {
    if (i >= subgraph_input_names.size()) {  // implicit inputs
      const auto& location = utils::FindMemoryInfoForValue(session_state, feed_names[i]);
      feed_locations[i] = location.device;
    } else {
      feed_locations[i] = default_location.device;
    }
  }

  std::unique_ptr<FeedsFetchesManager> ffm;
  ORT_RETURN_IF_ERROR(FeedsFetchesManager::Create(feed_names, subgraph_output_names,
                                                  subgraph_session_state.GetOrtValueNameIdxMap(), ffm));
  ORT_RETURN_IF_ERROR(utils::InitializeFeedFetchCopyInfo(subgraph_session_state, *ffm));

  // setup the locations where we want the subgraph output to end up on
  std::vector<const OrtMemoryInfo*> fetch_locations;
  fetch_locations.reserve(num_subgraph_outputs);

  // past state need to be where we can feed them in to the next iteration, so set the fetch location to match the feed location.
  for (int i = 0; i < num_subgraph_outputs; ++i) {
    fetch_locations.push_back(&default_location);
  }

  utils::FinalizeFeedFetchCopyInfo(*ffm, feed_locations, fetch_locations);

  feeds_fetches_manager_ = std::move(ffm);

  // Check subgraph only need once so put in Setup function.
  auto& inputs = subgraph.GetInputs();
  auto& outputs = subgraph.GetOutputs();
  ORT_RETURN_IF_ERROR(Validate(inputs, outputs));

  return Status::OK();
}

const IExecutionProvider* Subgraph::GetProvider() const {
  const ExecutionProviders& providers = session_state_->GetExecutionProviders();
  const IExecutionProvider* cpu_provider = providers.Get(onnxruntime::kCpuExecutionProvider);
  const IExecutionProvider* cuda_provider = providers.Get(onnxruntime::kCudaExecutionProvider);
  const IExecutionProvider* provider = cuda_provider ? cuda_provider : cpu_provider;
  return provider;
}

Status Subgraph::GetParameters(const ONNX_NAMESPACE::TensorShapeProto* past_shape,
                               const ONNX_NAMESPACE::TensorShapeProto* logits_shape,
                               bool merged_past) {
  if (merged_past) {
    // Mereged Past state shape is like (2, batch_size, 12, past_seq_len, 64). Here 12 and 64 are constants of num_heads and hidden_size/num_heads.
    ORT_RETURN_IF(past_shape->dim_size() != 5, "subgraph past state is expected to have 5 dimension, got ",
                  past_shape->dim_size());
    ORT_RETURN_IF(!past_shape->dim(0).has_dim_value() || past_shape->dim(0).dim_value() != 2,
                  "subgraph past state dimension 0 shall have length of 2");

    ORT_RETURN_IF(!past_shape->dim(2).has_dim_value() || past_shape->dim(2).dim_value() <= 0,
                  "subgraph past state dimension 2 shall have a positive value for number of heads");

    ORT_RETURN_IF(!past_shape->dim(4).has_dim_value() || past_shape->dim(4).dim_value() <= 0,
                  "subgraph past state dimension 4 shall have a positive value for hidden size per head");
    this->num_heads = static_cast<int>(past_shape->dim(2).dim_value());
    this->head_size = static_cast<int>(past_shape->dim(4).dim_value());
  } else {
    // Past state shape is like (batch_size, 12, past_seq_len, 64). Here 12 and 64 are constants of num_heads and hidden_size/num_heads.
    ORT_RETURN_IF(past_shape->dim_size() != 4, "subgraph output present_key_self_0 is expected to have 4 dimension, got ",
                  past_shape->dim_size());

    ORT_RETURN_IF(!past_shape->dim(1).has_dim_value() || past_shape->dim(1).dim_value() <= 0,
                  "subgraph past state dimension 2 shall have a positive value for number of heads");

    ORT_RETURN_IF(!past_shape->dim(3).has_dim_value() || past_shape->dim(3).dim_value() <= 0,
                  "subgraph past state dimension 4 shall have a positive value for hidden size per head");
    this->num_heads = static_cast<int>(past_shape->dim(1).dim_value());
    this->head_size = static_cast<int>(past_shape->dim(3).dim_value());
  }

  // Logits shape is like (batch_size, seq_len, 50257). Here 50257 is the vocabulary size.
  ORT_RETURN_IF(logits_shape->dim_size() != 3, "subgraph logits output is expected to have 3 dimension, got ",
                logits_shape->dim_size());

  ORT_RETURN_IF(!logits_shape->dim(2).has_dim_value() || logits_shape->dim(2).dim_value() <= 0,
                "subgraph past state dimension 2 shall have a positive value for vocabulary size");

  this->vocab_size = static_cast<int>(logits_shape->dim(2).dim_value());

  return Status::OK();
}

}  // namespace transformers
}  // namespace contrib
}  // namespace onnxruntime
