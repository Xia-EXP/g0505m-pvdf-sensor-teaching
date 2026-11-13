# G0505M / LDT0-028K 传感器教学项目

本仓库整理两条面向课堂演示的传感器项目：

- **G0505M 手掌受力教具**：通过厂家串口采集器读取 25 路压阻式估算值，提供实时可视化、实验引导、数据分析、回放和已知载荷标定工具。
- **LDT0-028K 动态触摸教具**：使用压电膜、PVA103 电荷放大模块和 Raspberry Pi Pico，采集动态触摸、敲击、弯曲和释放信号，并通过 USB 二进制协议与 TCP NDJSON 模拟机器人链路连接。

## 目录

- `source/g0505m-v2.0.1/`：G0505M 2.0.1 Python/PySide6 源码、测试和构建脚本。
- `source/pvdf-g0505m-demo/`：PVDF 与 G0505M 的演示源码，含 `pvdf_teaching` 和 `grip_teaching` 模块。
- `docs/`：BOM、接线、机械装配、标定、课堂脚本、通信协议和验收文档。
- `hardware/3d-printed-brackets/`：G0505M 增高支架 STL 模型及打印说明。
- `release/`：Pico UF2 固件和校验文件。Windows 便携包体积较大，未放入 Git 历史。

## 运行 G0505M 源码

需要 Python 3.12（支持 3.12–3.13）：

```powershell
cd source/g0505m-v2.0.1/desktop
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe run_g0505m.py
```

没有实物时可使用软件内置示例。连接实物前请先阅读 `docs/g0505m/README.md`，确认串口、电源和接线。

## 运行 PVDF 源码

进入 `source/pvdf-g0505m-demo/`，按其中 README 和 `requirements.txt` 创建 Python 3.12 虚拟环境。PVDF 项目默认面向动态变化信号；压电膜不适合在不增加称重传感器的情况下测量持续静态压力。

## 物理和安全边界

G0505M 的 25 路数值是采集器提供的局部估算值，不能把 25 点相加当作整手握力，也不能未经标定换算为牛顿或帕斯卡。LDT0-028K 的动态强度是相对量，不能冒充静态压力。真实机器人输出应保持关闭，并使用人工使能和急停。

仓库内的第三方模型、厂商协议和引用资料保留其原始许可与权利声明；请查看对应目录中的 NOTICE/LICENSE 文件。

