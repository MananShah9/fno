import sys
import asyncio

async def run():
    if len(sys.argv) < 2:
        print("Usage: python main.py [worker|cli]")
        print("No mode specified. Defaulting to starting the interactive CLI...")
        from cli import cli_main
        await cli_main()
        return

    mode = sys.argv[1].lower()
    if mode == "worker":
        from worker import worker_loop
        await worker_loop()
    elif mode == "cli":
        from cli import cli_main
        await cli_main()
    else:
        print(f"Unknown mode: {mode}. Use 'worker' or 'cli'.")

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user. Exiting...")
