# LDT0-028K 动态触摸数据教学装置

本项目实现一条可直接演示的数据链：

`手指触摸 → LDT0-028K 动态形变 → PVA103 模拟电压 → Pico 2 kS/s 采样 → USB 二进制协议 → 电脑处理 → TCP JSON → 模拟机器人 ACK`

## 直接使用

- Windows 10/11 便携版：[PVDF动态触摸教具-Windows-x64.zip](release/PVDF动态触摸教具-Windows-x64.zip)
- Raspberry Pi Pico 固件：[ldt0_touch_teaching.uf2](release/ldt0_touch_teaching.uf2)
- 发布文件校验值：[SHA256SUMS.txt](release/SHA256SUMS.txt)
- 完整接线和装配入口：[docs/README.md](docs/README.md)

第一次没有硬件也能演示：解压 Windows 包，保留整个目录，运行 `PVDF动态触摸教具.exe`。程序默认启动合成 PVDF 波形、本机 TCP 机器人 `127.0.0.1:18080` 和 20 Hz 数据发送。

连接实物时，按住 Pico 的 `BOOTSEL` 键插入 USB，把 UF2 拖入 `RPI-RP2` 磁盘；固件重启后，在上位机选择“自动查找 Pico”或指定 COM 口。

## 必须按此接线

| 信号 | 连接 |
| --- | --- |
| PVA103 `VCC` | Pico `3V3(OUT)`，物理 36 脚 |
| PVA103 `GND` | Pico `GND`，物理 38 脚 |
| PVA103 `OUT` | 串联 2.2 kΩ 后到 `GP26/ADC0`，物理 31 脚 |
| 100 nF 电容 | 接在 GP26 一侧与物理 38 脚 GND 之间 |
| PVA103 `TTL` | 不接，端头绝缘 |
| LDT0-028K 屏蔽线版黑线/屏蔽端 | PVA103 输入 `GND` / `-IN` |
| LDT0-028K 另一根信号线 | PVA103 `IN+` |

不要用 Pico 的 `VBUS` 5 V 给 PVA103 供电后再把 OUT 直接接 ADC；这可能超过 RP2040 模拟输入范围。完整逐针图、实物方向辨认和首次上电检查见 [BOM 与逐针接线](docs/01_BOM与逐针接线.md)。

## 重要物理边界

LDT0-028K 是压电膜，适合检测触碰、敲击、弯曲和释放等动态变化。手指按住不动后数值自然回落，是压电传感器的物理特性，不表示手指已经松开，也不能据此输出持续静态压力。

因此界面只显示 ADC、电压、变化电压、包络和 `0–100% 动态触摸强度`，不显示未经测力标定的牛顿或帕斯卡。若课程需要持续力，必须增加称重传感器/HX711 或其他静态力传感器。

## 源码结构

- [desktop](desktop/README.md)：Python 3.12、PySide6、pyqtgraph、串口、信号处理、记录/回放和 TCP 模拟机器人。
- [firmware](firmware/README.md)：Pico SDK C++ 固件、USB CDC 二进制协议和主机协议测试。
- [docs](docs/README.md)：采购、逐针接线、硬限位结构、增益、标定、课堂脚本、协议和验收表。

## 当前验证状态

已完成自动化单元测试、Qt 离屏全链路冒烟、Windows 便携包构建、固件主机协议测试以及 Pico SDK/ARM GCC 真实交叉编译。最终命令和结果见 [测试与验收](docs/06_测试与验收.md)。

尚未因为软件测试而假装完成硬件验收。以下项目必须拿到实际 LDT0-028K、PVA103 和 Pico 后执行：连续 30 分钟无应用层丢帧、正常重触不削顶、固定动作 20 次峰值重复性目标 ±15%、实机界面帧率和从事件识别到机器人 ACK 的 P95 延迟。
