# 1. 使用官方 Python 轻量级镜像
FROM python:3.9-slim

# 2. 设置工作目录
WORKDIR /app

# 3. 设置时区为上海 (解决时间显示问题)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 4. 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 复制项目所有代码到容器
COPY . .

# 6. 创建数据目录 (用于挂载数据库)
RUN mkdir -p /app/data

# 7. 暴露端口
EXPOSE 5000

# 8. 启动命令
CMD ["python", "app.py"]