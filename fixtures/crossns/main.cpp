#include "api.hpp"
#include <thread>

namespace app {
namespace detail {
void Worker() {
  // work
}
}

void Start() {
  detail::Worker();
  std::thread(detail::Worker);
}
}

int main() {
  app::Start();
  return 0;
}
