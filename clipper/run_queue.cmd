@echo off
rem Task Scheduler entry point for the queue worker. Extra arguments go to run_queue.py.
cd /d "%~dp0.."
if not exist .worker mkdir .worker
uv run --project video-use python clipper/run_queue.py %* >> .worker\task.log 2>&1
