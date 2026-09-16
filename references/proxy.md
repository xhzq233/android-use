# android-use Proxy 参考

proxy 是可选能力，要求本机已有 `mitmdump`：

```bash
android-use proxy doctor -s SERIAL
```

工具直接检测可执行文件；不在 `PATH` 时设置
`ANDROID_USE_MITMDUMP=/path/to/mitmdump`。缺少 mitmdump 不影响其他命令。

## 抓包

```bash
android-use proxy start -s SERIAL --port 18081
android-use proxy status -s SERIAL
android-use proxy dump -s SERIAL --format json
android-use proxy dump -s SERIAL --format har -o /tmp/capture.har
android-use proxy stop -s SERIAL --port 18081
```

默认链路是设备宿主机 `127.0.0.1:PORT`、`adb reverse` 和设备全局代理
`127.0.0.1:PORT`。`stop` 恢复启动前的设备代理并移除 reverse。settings 请求在 App
启动前开 proxy；AI 请求在触发生成前开。

## HTTPS

```bash
android-use proxy cert status -s SERIAL
android-use proxy cert instructions -s SERIAL
android-use proxy cert push -s SERIAL
```

量产设备通常需要在系统设置手工安装用户 CA；debug App 还要信任用户证书。证书
pinning 可能继续阻止解密。`install-root` 只适用于明确授权的 root 测试设备，不会由
`proxy start` 自动执行。

## Mock

```bash
android-use proxy start -s SERIAL --mock rules.json
android-use proxy mock -s SERIAL rules2.json
```

规则按 URL 子串顺序匹配，首个命中直接返回指定响应：

```json
[
  {"url": "/v1/example", "status": 200, "body": "{\"items\":[]}"}
]
```

URL 不要写得过宽。Mock 是主动改变服务响应的测试能力，结论仍需结合 UI、logcat 和
请求记录。

媒体注入不再由 android-use 包装；使用
[ADB 文件和媒体命令](adb.md#截图和文件)，并只操作隔离测试目录。
