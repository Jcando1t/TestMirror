# 网页评估复验

在仓库根目录运行以下 PowerShell 命令。仓库内的示例自带 pytest 测试，使用默认的离线分析即可。

```powershell
uv sync --locked
(Resolve-Path 'examples/interview_demo/project').Path
uv run --locked python -m uvicorn src.api.fastapi_server:app --host 127.0.0.1 --port 8000
```

1. 打开 <http://127.0.0.1:8000/>，选择「新建评估」，填入上一条命令输出的绝对路径，点击「开始评估」。
2. 示例应显示 **2 通过、1 失败**。在「用例结果」中筛选失败项，可以找到参数化用例 `test_free_shipping_threshold[5000-0]`。测试输出显示实际运费为 500 分，预期为 0 分。
3. 打开「运行记录」，重新打开这份报告，确认报告 ID、结果和用例计数一致。
4. 复制示例项目，在副本的 `shipping.py` 中把免运费条件从 `> 5000` 改为 `>= 5000`，再评估副本。预期 **3 通过、0 失败**。
5. 打开「回归对比」，分别选择原始报告和修复后的报告。应看到 **1 条已修复、0 条新失败**。展开「评测依据」可以核对两份报告的运行环境、项目路径和代码指纹。使用副本时，会显示项目路径不同的提示。

需要验证命令行和持久化时，可在仓库根目录运行 `uv run --locked python scripts/validate_workflow.py`。离线诊断和超时回退的完整演示见[示例说明](../../examples/interview_demo/README.md)。
