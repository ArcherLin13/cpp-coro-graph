#include "exec_task.hpp"

// Simulated device entry (name matched by devices.json)
exec::task<void> RunOnNpu(int workload) {
  // pretend enqueue
  (void)workload;
  co_return;
}

exec::task<void> DoCpuWork() {
  co_return;
}

exec::task<void> A() {
  co_await RunOnNpu(42);
  co_return;
}

exec::task<void> Caller() {
  co_await A();
  co_await DoCpuWork();
  co_return;
}

// OpenCL-ish host glue — should tag gpu via clEnqueue
void LaunchOpenCl() {
  clEnqueueNDRangeKernel(nullptr, nullptr, 0, nullptr, nullptr, nullptr, 0, nullptr, nullptr);
}
