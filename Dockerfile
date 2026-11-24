FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html ./
ENV DB_PATH=/data/sales.db
VOLUME /data
EXPOSE 8080
CMD ["python", "-u", "app.py"]
