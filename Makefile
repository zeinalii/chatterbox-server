HOST ?= 0.0.0.0
PORT ?= 7860
DEVICE ?= auto

PID_FILE := .chatterbox-server.pid
LOG_FILE := .chatterbox-server.log

.PHONY: up down status logs

up:
	@if [ -f "$(PID_FILE)" ] && kill -0 "$$(cat "$(PID_FILE)")" 2>/dev/null; then \
		echo "Chatterbox server already running on pid $$(cat "$(PID_FILE)")"; \
	else \
		nohup ./chatterbox --serve --host "$(HOST)" --port "$(PORT)" --device "$(DEVICE)" >"$(LOG_FILE)" 2>&1 & \
		echo $$! >"$(PID_FILE)"; \
		echo "Chatterbox server starting on http://$(HOST):$(PORT)"; \
		ips="$$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$$' | grep -v '^127\.' || true)"; \
		if [ -n "$$ips" ]; then \
			echo "Use from another device:"; \
			for ip in $$ips; do echo "  ./chatterbox story.txt --url $$ip:$(PORT) --out audio.wav"; done; \
		else \
			echo "Could not detect a LAN IP automatically. Run: hostname -I"; \
		fi; \
		echo "Logs: $(LOG_FILE)"; \
	fi

down:
	@if [ -f "$(PID_FILE)" ] && kill -0 "$$(cat "$(PID_FILE)")" 2>/dev/null; then \
		kill "$$(cat "$(PID_FILE)")"; \
		echo "Stopped Chatterbox server pid $$(cat "$(PID_FILE)")"; \
		rm -f "$(PID_FILE)"; \
	else \
		echo "Chatterbox server is not running"; \
		rm -f "$(PID_FILE)"; \
	fi

status:
	@if [ -f "$(PID_FILE)" ] && kill -0 "$$(cat "$(PID_FILE)")" 2>/dev/null; then \
		echo "Chatterbox server running on pid $$(cat "$(PID_FILE)")"; \
	else \
		echo "Chatterbox server is not running"; \
	fi

logs:
	@tail -f "$(LOG_FILE)"
