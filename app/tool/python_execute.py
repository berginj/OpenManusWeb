import asyncio
import os
import sys
from typing import Dict

from app.tool.base import BaseTool


class PythonExecute(BaseTool):
    """Execute Python in a separate process with a timeout."""

    name: str = "python_execute"
    description: str = "Executes Python code string. Note: Only print outputs are visible, function return values are not captured. Use print statements to see results."
    parameters: dict = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The Python code to execute.",
            },
        },
        "required": ["code"],
    }

    async def execute(
        self,
        code: str,
        timeout: int = 5,
    ) -> Dict:
        """
        Executes the provided Python code with a timeout.

        Args:
            code (str): The Python code to execute.
            timeout (int): Execution timeout in seconds.

        Returns:
            Dict: Contains 'output' with execution output or error message and 'success' status.
        """
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c',
            "import sys; exec(compile(sys.stdin.read(), '<python_execute>', 'exec'))",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(code.encode('utf-8')), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"observation": f"Execution timeout after {timeout} seconds", "success": False}
        output = stdout.decode('utf-8', errors='replace').replace('\r\n', '\n')
        errors = stderr.decode('utf-8', errors='replace').replace('\r\n', '\n')
        if process.returncode:
            return {"observation": output + errors, "success": False}
        return {"observation": output + errors}
