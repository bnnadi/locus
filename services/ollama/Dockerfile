FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:8080

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]