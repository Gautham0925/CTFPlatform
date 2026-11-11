# Minimal test challenge for runner integration tests.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
RUN mkdir -p /www && echo "challenge-ok" > /www/index.html
EXPOSE 8080
CMD ["sh", "-c", "while true; do printf 'HTTP/1.0 200 OK\\r\\n\\r\\nchallenge-ok\\n' | nc -l -p 8080; done"]
