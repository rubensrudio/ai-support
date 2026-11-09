import { StreamEvent } from './chat.models';

/** Result of one incremental parsing step (CT-51). */
export interface SseParseResult {
  events: StreamEvent[];
  /** Incomplete tail to pass back as `buffer` together with the next chunk. */
  rest: string;
}

const STREAM_EVENT_TYPES: ReadonlySet<string> = new Set<StreamEvent['type']>([
  'start',
  'token',
  'tool_start',
  'tool_end',
  'citation',
  'done',
  'error',
]);

/**
 * Incremental parser for the `text/event-stream` body of CT-76.
 *
 * Joins `buffer + chunk`, emits every complete block (terminated by a blank line; `\n`, `\r\n`
 * and `\r` line endings are accepted) and returns the incomplete tail in `rest`. Multiple
 * `data:` lines are joined with `\n`; comments (`:`) and blocks without `data` are skipped.
 * The payload is JSON whose `type` names the event; a block whose JSON is invalid, whose `type`
 * is unknown, or whose `event:` line diverges from `type` is ignored.
 */
export function parseSse(chunk: string, buffer: string): SseParseResult {
  let text = buffer + chunk;
  // A trailing `\r` may be the first half of a `\r\n` split between chunks: hold it back.
  let held = '';
  if (text.endsWith('\r')) {
    held = '\r';
    text = text.slice(0, -1);
  }
  text = text.replace(/\r\n?/g, '\n');

  const boundary = text.lastIndexOf('\n\n');
  if (boundary < 0) {
    return { events: [], rest: text + held };
  }

  const events: StreamEvent[] = [];
  for (const block of text.slice(0, boundary).split('\n\n')) {
    const event = parseBlock(block);
    if (event) events.push(event);
  }
  return { events, rest: text.slice(boundary + 2) + held };
}

function parseBlock(block: string): StreamEvent | null {
  let eventName: string | null = null;
  const data: string[] = [];

  for (const line of block.split('\n')) {
    if (line === '' || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') eventName = value;
    else if (field === 'data') data.push(value);
  }

  if (data.length === 0) return null;

  let payload: unknown;
  try {
    payload = JSON.parse(data.join('\n'));
  } catch {
    return null;
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return null;

  const type = (payload as Record<string, unknown>)['type'];
  if (typeof type !== 'string' || !STREAM_EVENT_TYPES.has(type)) return null;
  if (eventName !== null && eventName !== type) return null;

  return payload as StreamEvent;
}
