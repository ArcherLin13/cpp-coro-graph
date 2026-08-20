#pragma once
namespace mod {
void Alpha();
void Beta(int x);

class Service {
public:
  Service();
  ~Service();
  void Start();
  void Stop() const;
  int Run(int n) { return n + 1; }
};

exec::task<void> AsyncWork();
}
