#pragma once
#include "core/framework/stream_handles.h"
#include "gsl/gsl"

namespace onnxruntime {
class SessionState;

class DeviceStreamCollectionImpl;
// a collection of indexed device stream instances.
// the index is the same as the logic sequence index in the execution plan.
// this collection may be cached and reused for future iterations.
class DeviceStreamCollection {
 public:
  DeviceStreamCollection(size_t num_streams, const SessionState& sess_state);
  ~DeviceStreamCollection();
  // Set the device stream instance at given index.
  // and set the current collection as the owner of the device stream.
  void SetDeviceStream(size_t, std::unique_ptr<Stream> stream);
  // user an external device stream instance at given index.
  // the current collection is not the owner.
  // this is mainly used in subgraph execution, when we want the
  // subgraph nodes execute on the same stream as parent node.
  void SetDeviceStream(size_t, Stream* stream);
  // Get the index device stream instances.
  gsl::span<Stream*> GetStreams() const;
  // get the number of device stream instances.
  size_t NumStreams() const;
  // Since the collection may be reused for future iteration,
  // This API is used to cleanup some resources at the end of an iteration.
  Status CleanUp();

 private:
  std::unique_ptr<DeviceStreamCollectionImpl> impl_;
};
}  // namespace onnxruntime