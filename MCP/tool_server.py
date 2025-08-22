import json
import os
import sys
import asyncio
import uvicorn
from typing import Dict, Any, Optional, Tuple
from fastapi import FastAPI, HTTPException
import logging
import traceback
import threading
import types
from mcp_manager import MCPManager
from io_manage import ThreadOutputManager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pydantic import BaseModel

import time


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CodeRequest(BaseModel):
    code: str
    timeout: Optional[int] = 180  # 默认超时30秒

class CodeResponse(BaseModel):
    output: str
    error: Optional[str]
    execution_time: float
  
manager = MCPManager()
app = FastAPI()
output_manager = ThreadOutputManager()
execution_semaphore = asyncio.Semaphore(2000)  
executor = ThreadPoolExecutor(max_workers=1000)  


@app.post("/call_tool/{tool_name}")
async def create_tool_task(tool_name: str, tool_args: Dict):
    print(f"=============={tool_name}===============")
    tool_names = manager.get_toolnames()
    if tool_name not in tool_names:
        raise HTTPException(404, "Tool not found")
    

    result = None
    status = False

    try:
        result = await manager.call_tool(
            tool_name,
            tool_args
        )
        status = True
        
        
        final_result = []
        for item in result:
            try:
                json_obj = json.loads(item)
                final_result.append(json_obj)
            except Exception as e:
                final_result.append(item)
        result = final_result

    except Exception as e:
        result = f"Error: {str(e)}\n\n{traceback.format_exc()}\n\ntool name: {tool_name}\n\n tool args: {tool_args}"
        status = False
        
    finally:
        return {"status": status, "result": result[0]}




async def execute_python_code(code: str, timeout: int) -> Tuple[str, Optional[str], float]:
    
    loop = asyncio.get_event_loop()
    start_time = loop.time()

    try:
        execution_time, output, error = await loop.run_in_executor(executor, _execute_code_safely, code, timeout)
    except Exception as e:
        error = f"Execution failed: {str(e)}"
        output = ""
        logger.error(f"Unexpected error: {error}", exc_info=True)
        execution_time = loop.time() - start_time
        
    return output, error, execution_time

def _execute_code_safely(code: str, timeout: int) -> Tuple[str, Optional[str]]:
    
    logger.info(f"Executing in thread {threading.current_thread().ident}, process {os.getpid()}")

    capture = output_manager.get_capture()

    error = None
    output_value = None
    error_value = None

    start_time = time.time()

    single_executor = ThreadPoolExecutor(max_workers=1)

    def run_code():
        module = types.ModuleType("dynamic_module")
        module.__dict__.update({
            'print': lambda *args, **kwargs: capture.write(
                ' '.join(str(arg) for arg in args) + ('\n' if kwargs.get('end', '\n') else '')
            ),
            '__builtins__': __builtins__,
            'sys': type('sys', (), {
                'stdout': capture,
                'stderr': capture,
                'stdin': None,
            })(),
        })
        exec(code, module.__dict__)
        return capture.get_stdout(), capture.get_stderr()

    try:
        future = single_executor.submit(run_code)
        output_value, error_value = future.result(timeout=timeout)

    except FutureTimeoutError:
        error = f"Execution timed out after {timeout} seconds"
        logger.warning(f"Code execution timeout: {timeout}s")
        error_value = error

    except SystemExit as se:
        error = f"Code called sys.exit({se.code})"
        if not capture.stderr.closed:
            capture.stderr.write(error)
        logger.warning(f"Code triggered SystemExit: {error}\n\n-----\n{code}")
        error_value = error

    except Exception as e:
        error = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
        
        if not capture.stderr.closed:
            capture.stderr.write(error)
        logger.warning(f"Code execution error: {error}\n\n-----\n{code}")
        error_value = error

    finally:
        if not capture.stdout.closed or not capture.stderr.closed:
            if output_value is None:
                output_value = capture.get_stdout()
            if error_value is None or error_value != error:
                error_value = capture.get_stderr()

    execution_time = time.time() - start_time

    return execution_time, output_value, error_value if error_value else None



@app.post("/execute", response_model=CodeResponse)
async def execute_code_handler(request: CodeRequest):

    if not request.code.strip():
        raise HTTPException(
            status_code=400,
            detail="Code cannot be empty"
        )
    
    logger.info(f"Executing code snippet (timeout: {request.timeout}s)")
    
    try:
        output, error, exec_time = await execute_python_code(
            request.code,
            request.timeout
        )
        
        logger.info(
            f"Code execution completed in {exec_time:.2f}s. "
            f"Output length: {len(output)}, Error: {bool(error)}"
        )
        
        return CodeResponse(
            output=output,
            error=error,
            execution_time=exec_time,
        )
        
    except Exception as e:
        logger.error(f"Unexpected server error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )



if __name__ == "__main__":
    import os

    PORT = os.getenv('PORT', 40001)

    uvicorn.run(
        "tool_server_session:app", 
        host="0.0.0.0", 
        port=int(PORT),
        lifespan="on",
        workers=1
        # reload=True
    )