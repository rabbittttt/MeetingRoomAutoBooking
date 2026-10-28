# MeetingRoomAutoBooking

Windows 会议室预约工具，支持到点抢订、每周固定任务及空闲候补。

## 工程结构

- `项目源码/`：预约核心、窗口、区域配置、构建工具及离线测试。
- `授权管理/`：管理员授权工具和使用说明。
- `AGENTS.md`：工程维护约定。

## 运行与构建

电脑需安装 Google Chrome，并能访问公司的会议室系统。

在 `项目源码` 中运行 `一键打包EXE.cmd`，选择所需区域；也可使用：

```powershell
python build_exe.py --profile F5
python build_exe.py --profile H7
python build_exe.py --profile B2a
```

区域资料在 `项目源码/deployment_profiles.py` 维护。普通使用者可勾选预置会议室。
预约起止时间采用半小时刻度；单独30分钟只点击一个时间格。

本仓库保留当前目录中的构建产物。源码更新后，需要重新打包才能将修改带入EXE；
各区域输出位于 `项目源码/dist/<区域>/`。

## 授权文件

账号授权JSON、签名验证文件、账号名单及签名私钥只在本地维护，不进入仓库。
打包、构建校验及启动自检不读取、生成、复制或覆盖外置授权JSON。
正常运行时，EXE仍需与管理员单独提供的 `账号授权.json` 放在同一目录。
更新程序时只替换EXE，保留使用者已有的授权文件。

具体设置和操作见 `项目源码/使用说明.txt`、`项目源码/区域打包说明.txt`。

## W3备用登录

高级设置可单独保存本机W3账号密码。原有自动登录优先，仅在已核验的
`https://login.huawei.com` 账号登录页使用备用凭据，一次运行最多自动提交一次密码。
验证码和二次验证由使用者手动完成。

凭据使用Windows当前用户加密，保存在
`%LOCALAPPDATA%\MeetingRoomAutoBooking\w3_credentials.dat`，各区域共用。
该文件及临时文件被Git忽略，不写入任务配置、不记录到日志、不参与构建和打包。
换电脑或Windows账号后重新设置；更新密码或删除凭据也在高级设置操作。

## W3备用登录

高级设置可单独保存本机W3账号密码。原有自动登录优先，仅在已核验的
`https://login.huawei.com` 账号登录页使用备用凭据，一次运行最多自动提交一次密码。
验证码和二次验证由使用者手动完成。

凭据使用Windows当前用户加密，保存在
`%LOCALAPPDATA%\MeetingRoomAutoBooking\w3_credentials.dat`，各区域共用。
该文件及临时文件被Git忽略，不写入任务配置、不记录到日志、不参与构建和打包。
换电脑或Windows账号后重新设置；更新密码或删除凭据也在高级设置操作。
