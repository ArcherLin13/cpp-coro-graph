#pragma once
namespace demo {

struct SosModel {
  exec::task<void> Init();
  exec::task<void> Load();
  exec::task<void> PreRun();
  exec::task<void> Run();
  exec::task<void> PostRun();

  // typical pattern: static entry builds *this* wrapper then runs stages
  static exec::task<void> Call(int id) {
    SosModel m;
    co_await m.Init();
    co_await m.Load();
    co_await m.PreRun();
    co_await m.Run();
    co_await m.PostRun();
    co_return;
  }
};

// free function variant (type comes from local var, not enclosing class)
exec::task<void> RunSosOnce() {
  SosModel m;
  co_await m.Init();
  co_await m.Run();
  co_await m.PostRun();
  co_return;
}

}  // namespace demo
