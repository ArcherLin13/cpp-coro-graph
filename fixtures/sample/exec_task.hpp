#pragma once
#include <cstdint>

namespace exec {
template <typename T>
struct task {};
}

exec::task<void> RunOnNpu(int workload);
exec::task<void> DoCpuWork();
exec::task<void> A();
exec::task<void> Caller();
