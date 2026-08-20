#include "mod.hpp"

namespace mod {
void Alpha() {
  Beta(1);
}

void Beta(int x) {
  (void)x;
  Service s;
  s.Start();
}

Service::Service() = default;
Service::~Service() = default;

void Service::Start() {
  Stop();
}

void Service::Stop() const {}

exec::task<void> AsyncWork() {
  co_await Alpha();
  co_return;
}
}
