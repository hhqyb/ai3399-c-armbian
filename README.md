# Signway AI3399-C Armbian

把 Armbian 的 Rock Pi 4A 最小镜像改成南京欣威视通 Signway AI3399-C（RK3399）可直接刷写的系统。

刷完不需要再手工喂狗，登录信息也不再显示 `Radxa ROCK Pi 4A`。

## 改了什么

- 外部 MCU 看门狗。原厂 Android 驱动在启动时把 I2C 地址 `0x70` 的寄存器 `0x1C` 写成 0 来停止它，运行中用寄存器 `0x20=1` 喂狗。Armbian 没有这个驱动，所以板子大约 300 秒会重启。镜像里的 `/etc/rc.local` 会在第一次启动时装上 `/usr/local/sbin/ai3399c-mcu-wdt` 和 systemd 服务，并立刻开始喂狗。之后每次开机由 `sysinit` 提前拉起。
- 只写这两只寄存器，不要读 MCU。读寄存器会把 `i2c-0`（`ff3c0000`）卡死，而这路总线还挂着 RK808 和 CPU 供电。
- 可见型号改成 `Signway AI3399-C`，主机名改成 `ai3399-c`。`BOARD=rockpi-4a` 和 `fdtfile=rockchip/rk3399-rock-pi-4a.dtb` 保持不变，否则 Armbian 的启动脚本和设备树文件名对不上。

## 获取 rockpi-4a-minimal.img

这是 Armbian 社区版 Rock Pi 4A 的原始镜像，不是本仓库生成的。`patch_image.py` 按这一版的目录结构改写，请不要换成更新的滚动构建。

- 板卡页面：<https://www.armbian.com/rockpi-4a/>
- 规格：Debian 13 Trixie，Minimal（CLI），kernel `current` 6.18.57，版本 `26.11.0-trunk.100`，2026-10-17
- 直接下载：<https://github.com/armbian/community/releases/download/26.11.0-trunk.100/Armbian_community_26.11.0-trunk.100_Rockpi-4a_trixie_current_6.18.57_minimal.img.xz>
- 发布页里的跳转地址：<https://dl.armbian.com/rockpi-4a/Trixie_current_minimal>
- 校验文件：<https://dl.armbian.com/rockpi-4a/Trixie_current_minimal.sha>，签名和种子把后缀换成 `.asc`、`.torrent`。这些跳转以后可能指向更新的构建；对不上时，以文件名中的 `26.11.0-trunk.100`、`6.18.57` 和下面的 SHA256 为准。

下载后核对压缩包：

- 大小：375580236 字节
- SHA256：`6af607deb109e13dc88be29696ce1ec8ae77ff80e9415f47d9e66e6854f37915`

解压后得到约 1.8GB 的 `.img`（1858076672 字节）。文件名可以保持官方名字，也可以改成 `rockpi-4a-minimal.img`，后面的命令都能用。Windows 用 7-Zip 解压；Linux 用 `xz -dk`。

## 生成镜像

```
python patch_image.py rockpi-4a-minimal.img --out ai3399-c-armbian.img
python patch_image.py ai3399-c-armbian.img --check
```

`patch_image.py` 会改设备树和 U-Boot 里的 model 字符串、`/etc/armbian-release`、主机名，以及 `/etc/rc.local`。ext4 元数据校验和会一起重算。文件系统必须是干净卸载的。

改好的镜像大约 1.8GB，超过 GitHub 单文件限制，所以没有放进这个仓库。

## 刷写

板子先进入能用 adb 的系统。这套板在 Android 侧的 SD 卡是 `/dev/block/mmcblk1`。

```
python flash_sd.py ai3399-c-armbian.img
```

默认使用 `D:\Software\platform-tools\adb.exe`（也可以用环境变量 `ADB`），刷完后从 `COM4` / 1500000 抓启动日志。不重启时加 `--no-reboot`。

## 目录

- `patch_image.py`：把上述修改写进 ext4 镜像
- `flash_sd.py`：通过 adb 把镜像写入 SD 卡
- `rootfs/etc/rc.local`：写进镜像的启动脚本
- `rootfs/usr/local/sbin/ai3399c-mcu-wdt`：喂狗程序，第一次启动时由 rc.local 装到系统里
- `rootfs/etc/systemd/system/ai3399c-mcu-wdt.service`：对应的 systemd 单元