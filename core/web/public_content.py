"""Fail closed before public HTTP headers or encoded text leave the application."""
import json
import logging

from starlette.responses import JSONResponse


class PublicContentMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from utils.public_branding import contains_private, UNAVAILABLE
        start, chunks = None, []
        textual = False
        blocked = False

        async def checked(message):
            nonlocal start, textual, blocked
            if message['type'] == 'http.response.start':
                start = message
                headers = {k.decode('latin1'): v.decode('latin1') for k, v in message['headers']}
                blocked = contains_private(headers)
                content_type = headers.get('content-type', '')
                textual = 'json' in content_type or content_type.startswith('text/')
                if not textual and not blocked:
                    await send(message)
            elif message['type'] == 'http.response.body':
                if textual:
                    chunks.append(message.get('body', b''))
                    if message.get('more_body'):
                        return
                    body = b''.join(chunks)
                    try:
                        content = json.loads(body) if 'json' in dict(start['headers']).get(b'content-type', b'').decode() else body.decode('utf-8')
                        blocked = blocked or contains_private(content)
                    except (UnicodeError, ValueError):
                        blocked = True
                    if not blocked:
                        await send(start)
                        await send({'type': 'http.response.body', 'body': body})
                        return
                if blocked:
                    if message.get('more_body'):
                        return
                    logging.getLogger('ajib.web').warning('public_content_blocked')
                    response = JSONResponse({'detail': UNAVAILABLE}, status_code=503,
                                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
                    await response(scope, receive, send)
                else:
                    await send(message)
        await self.app(scope, receive, checked)
