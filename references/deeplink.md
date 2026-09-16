# android-use DeepLink 参考

由 App 项目提供 URI 和断言。android-use 负责安全传递 URI，
并可组合 Activity、日志或 UI 断言：

```bash
android-use deeplink 'example://screen/<key>' --wait
android-use deeplink 'example://screen/<key>' \
  --wait \
  --expect-activity '^com\.example\.app/' \
  --assert-timeout 15s
```

可用断言：

```bash
--expect-activity REGEX
--expect-log REGEX
--expect-text TEXT
```

- 无断言时，成功只表示 Android Intent 调度成功，业务验证状态为 `unknown`。
- `--expect-log` 需要动作前已有健康的持续 logcat。
- `--expect-text` 依赖 stock UI tree；Lynx/自绘页面应改用 Activity、日志或后续截图。
- 断言失败自动生成 evidence；有意探测失败路径时可加 `--no-evidence`。

只需要系统 Intent 时，直接使用 [ADB DeepLink](adb.md#deeplink)。
