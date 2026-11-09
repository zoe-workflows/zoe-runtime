# Qualification fixture：最小可验证改动

## 目标与非目标

在仓库根目录创建文件 `qualification-output.txt`，写一行任意文本。
不修改其他文件，不提交，不推送。

## 验收标准

- AC-001：仓库根目录存在 `qualification-output.txt`；验证：`test -f qualification-output.txt`
