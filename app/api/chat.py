from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Literal, Optional, AsyncGenerator
from collections import deque
import json
import os
import asyncio
import logging
from dotenv import load_dotenv
from app.services.llm.factory import get_llm_client

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

router = APIRouter()

# ----------- Models -----------

class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    language: Optional[str] = "python"

class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class StartChunk(BaseModel):
    type: Literal["start"]
    usage: Optional[TokenUsage] = None

class TokenChunk(BaseModel):
    type: Literal["token"]
    token: str
    role: Literal["assistant"] = "assistant"
    index: int
    usage: Optional[TokenUsage] = None

class CodeStartChunk(BaseModel):
    type: Literal["code_start"]
    language: str
    usage: Optional[TokenUsage] = None

class CodeEndChunk(BaseModel):
    type: Literal["code_end"]
    usage: Optional[TokenUsage] = None

class ErrorChunk(BaseModel):
    type: Literal["error"]
    error: str
    code: str

class DoneChunk(BaseModel):
    type: Literal["done"]
    finish_reason: Literal["stop", "length", "function_call", "user_abort"]
    usage: Optional[TokenUsage] = None

# ----------- Chunk Senders -----------

async def send_chunk(chunk: BaseModel) -> str:
    return f"data: {json.dumps(chunk.model_dump())}\n\n"

async def send_start() -> str:
    return await send_chunk(StartChunk(type="start"))

async def send_token(token: str, index: int) -> str:
    await asyncio.sleep(0.01)  # Optional delay to simulate streaming
    return await send_chunk(TokenChunk(type="token", token=token, index=index))

async def send_code_start(language: str) -> str:
    return await send_chunk(CodeStartChunk(type="code_start", language=language))

async def send_code_end() -> str:
    return await send_chunk(CodeEndChunk(type="code_end"))

async def send_done(reason: Literal["stop", "length", "function_call", "user_abort"] = "stop") -> str:
    return await send_chunk(DoneChunk(type="done", finish_reason=reason))

async def send_error(error: str, code: str = "internal_error") -> str:
    return await send_chunk(ErrorChunk(type="error", error=error, code=code))

# ----------- Token Processor -----------
CODE_START_IDENTIFIER_SIZE = 11
CODE_END_IDENTIFIER_SIZE = 4

async def _handle_buffer_content(buffer: deque, content: str, index: int, state: dict[str, str], identifier: str, identifier_size: int, is_start: bool) -> AsyncGenerator[str, None]:
    if len(content) < identifier_size:
        return
    
    idx = content.find(identifier)
    if idx != -1:
        yield await send_token(content[:idx], index)
        if is_start:
            state['code_block_open'] = True
            yield await send_code_start("python")
        else:
            state['code_block_open'] = False
            state['has_found_code'] = True
            yield await send_code_end()
        
        buffer.clear()
        if idx + identifier_size < len(content):
            for c in content[idx + identifier_size:]:
                buffer.append(c)
    else:
        yield await send_token(content[:-(identifier_size-1)], index)
        buffer.clear()
        for c in content[-(identifier_size-1):]:
            buffer.append(c)

async def process_token(buffer: deque, token: str, index: int, state: dict[str, str]) -> AsyncGenerator[str, None]:
    if state['has_found_code']:
        yield await send_token(token, index)
        return

    for c in token:
        buffer.append(c)
    recent_str = "".join(buffer)

    if state['code_block_open']:
        async for chunk in _handle_buffer_content(buffer, recent_str, index, state, "```\n", CODE_END_IDENTIFIER_SIZE, False):
            yield chunk
    else:
        async for chunk in _handle_buffer_content(buffer, recent_str, index, state, "```python\n", CODE_START_IDENTIFIER_SIZE, True):
            yield chunk

    index += 1

# ----------- Route -----------

@router.post("/chat")
async def chat(request: ChatRequest):
    if not request.messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    async def generate_stream_response():
        try:
            yield await send_start()

            client = get_llm_client()
            messages = [{"role": m.role, "content": m.content} for m in request.messages]

            index = 0
            buffer = deque()
            state = {
                'code_block_open': False,
                'has_found_code': False
                }
            async for chunk in client.chat_completion(messages=messages):
                try:
                    content = chunk.choices[0].delta.content
                except (AttributeError, IndexError):
                    continue

                if not content:
                    continue

                async for response_chunk in process_token(buffer, content, index, state):
                    yield response_chunk
                index += 1

            yield await send_done()

        except Exception as e:
            logger.error(f"Error in stream response: {e}")
            yield await send_error(str(e))

    return StreamingResponse(
        generate_stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
