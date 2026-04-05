# 建德本地信息发布系统

一个基于 Flask 的本地信息发布网站，支持站长发布信息（含图片），用户浏览信息。使用 SQLite 数据库存储，账号密码安全加密。

## 功能特点

### 前台页面
- 📱 移动端优化设计
- 🏷️ 支持分类筛选（出售、求购、招聘、出租房屋、出售房屋、其他）
- 📞 显示站长联系方式
- 🖼️ 图片瀑布流展示，点击查看大图
- 🔍 只读浏览，用户无法发布

### 后台管理
- 🔐 安全的账号密码登录（密码哈希加密）
- ✏️ 发布新信息（支持上传最多 9 张图片）
- 🖼️ 图片实时预览，拖拽删除
- 🗑️ 删除已发布信息（自动删除关联图片）
- 📋 管理所有信息

## 项目结构

```
jd.windyzhou.org/
├── app.py                 # Flask 后端应用
├── requirements.txt       # Python 依赖
├── posts.db              # SQLite 数据库（自动生成）
├── uploads/              # 图片上传目录（自动生成）
├── templates/
│   ├── index.html        # 前台页面
│   └── admin.html        # 后台管理页面
└── README.md             # 使用说明
```

## 安装步骤

### 1. 安装 Python
确保已安装 Python 3.7 或更高版本。

检查 Python 版本：
```bash
python --version
```

### 2. 安装依赖
在项目目录下运行：
```bash
pip install -r requirements.txt
```

### 3. 配置账号密码和联系方式（推荐使用环境变量）

**方法一：使用环境变量（推荐）**

创建 `.env` 文件：
```bash
SECRET_KEY=your-secret-key-here
ADMIN_USERNAME=admin
ADMIN_PASSWORD=123456
ADMIN_CONTACT=周秋良:手机:15868404601,微信同号
```

**方法二：直接修改代码**

打开 `app.py` 文件，修改第 23-28 行：
```python
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '你的账号')
admin_password = os.getenv('ADMIN_PASSWORD', '你的密码')
ADMIN_CONTACT = os.getenv('ADMIN_CONTACT', "你的联系方式")
```

### 4. 启动服务器
```bash
python app.py
```

启动成功后会显示：
```
==================================================
服务器启动成功！
前台页面: http://127.0.0.1:5000/
后台管理: http://127.0.0.1:5000/admin
默认账号: admin
默认密码: 123456
==================================================
```

## 使用说明

### 访问前台
在浏览器打开：`http://127.0.0.1:5000/`
- 查看所有已发布的信息
- 使用分类筛选器筛选信息
- 查看站长联系方式

### 访问后台管理
在浏览器打开：`http://127.0.0.1:5000/admin`
1. 输入账号密码登录
2. 发布新信息：
   - 选择分类
   - 填写标题和内容
   - 输入联系方式
   - 可选：点击"选择图片"上传图片（最多9张，支持 png/jpg/jpeg/gif/webp）
   - 点击"发布信息"
3. 管理已发布的信息：查看、删除信息

### 手机访问
如果要让局域网内的手机访问：
1. 查看电脑的 IP 地址（Windows: `ipconfig`，Mac/Linux: `ifconfig`）
2. 手机连接到同一个 WiFi
3. 在手机浏览器访问：`http://你的电脑IP:5000/`

例如：`http://192.168.1.100:5000/`

## 安全性说明

✅ **已实现的安全措施：**
- 密码使用 Werkzeug 的 `generate_password_hash` 进行哈希加密
- 账号密码存储在服务器端，客户端无法看到
- 使用 Session 管理登录状态
- 所有管理操作需要登录验证

⚠️ **注意事项：**
- 默认使用 HTTP 协议，不建议在公网使用
- 如需公网部署，建议配置 HTTPS
- 修改 `app.secret_key` 为随机字符串

## 数据存储

- **数据库：** 使用 SQLite 数据库（`posts.db`）存储所有信息
  - 服务器启动时自动创建数据库和表结构
  - 支持高并发访问
  - 数据持久化存储

- **图片文件：** 上传的图片保存在 `uploads/` 目录
  - 文件名采用 UUID 命名，避免冲突
  - 删除信息时自动删除关联图片
  - 支持格式：png, jpg, jpeg, gif, webp
  - 单文件最大 16MB

## 常见问题

### 1. 如何修改端口？
修改 `app.py` 最后一行：
```python
app.run(debug=True, host='0.0.0.0', port=5000)  # 修改 port 参数
```

### 2. 如何重置密码？
修改 `app.py` 第 14 行的密码，重启服务器即可。

### 3. 数据丢失怎么办？
定期备份 `posts.db` 数据库文件和 `uploads/` 图片目录。

### 3.1 如何迁移旧数据（从 JSON 到 SQLite）？
如果你之前使用的是 JSON 版本，可以手动导入数据到数据库，或联系开发者获取迁移脚本。

### 4. 如何部署到服务器？
- 使用 Gunicorn 或 uWSGI 作为生产服务器
- 配置 Nginx 作为反向代理
- 建议使用 HTTPS（Let's Encrypt）

## 技术栈

- **后端：** Python + Flask
- **前端：** HTML + CSS + JavaScript
- **数据库：** SQLite 3
- **文件存储：** 本地文件系统
- **安全：** Werkzeug 密码哈希 + Session 管理
- **图片处理：** Werkzeug 文件上传

## 开发者

如需修改样式或功能，请编辑：
- `templates/index.html` - 前台页面
- `templates/admin.html` - 后台管理页面
- `app.py` - 后端逻辑

---

**祝使用愉快！** 如有问题，请检查控制台输出的错误信息。
