# 强脑 Revo2 灵巧手模型来源说明

- 来源仓库：https://github.com/BrainCoTech/revo2_description
- 版本：提交 `92cc697c7fa691db59404ce52344f3969a5ef7a6`（2026-08-20）
- 许可证：Apache License 2.0，全文见同目录 `LICENSE`
- 版权归 BrainCo Inc.（强脑科技）所有

本教具只复制了 `urdf/revo2_left_hand.urdf`、`urdf/revo2_right_hand.urdf` 和 `meshes/` 下的 STL 文件，未修改文件内容。
程序在运行时把 URDF 里的 `package://revo2_description/meshes/...` 路径改为读取本目录下的文件。

用途边界：模型只用于屏幕显示。软件不连接、不控制任何真实的 Revo2 机械手；
手指弯曲是根据 G0505M 受力推算的示意动作，不是测量得到的关节角。
