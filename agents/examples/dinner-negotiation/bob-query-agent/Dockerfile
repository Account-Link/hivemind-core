# Base off hivemind-agent-base so the sandbox builder doesn't need to
# resolve pip + apt at build time. The base image already has httpx
# pinned. If you need extra dependencies, layer them after this line.
#
# A bare `FROM python:3.12-slim` + `pip install httpx` Dockerfile DOES
# build standalone but FAILS inside the sandbox builder's network-
# constrained build context — pip cannot reach PyPI from there. Using
# the prebuilt base avoids that whole class of issue.
FROM hivemind-agent-base:latest

WORKDIR /app
COPY agent.py .
COPY b-private/ ./b-private/
CMD ["python", "agent.py"]
