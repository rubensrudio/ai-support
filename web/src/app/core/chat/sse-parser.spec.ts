import { StreamEvent } from './chat.models';
import { parseSse } from './sse-parser';

const TOKEN: StreamEvent = { type: 'token', text: 'Hello' };
const TOOL_START: StreamEvent = {
  type: 'tool_start',
  call_id: 'c1',
  tool: 'get_order',
  label_key: 'step.get_order',
};

function block(event: StreamEvent, eol = '\n'): string {
  return `event: ${event.type}${eol}data: ${JSON.stringify(event)}${eol}${eol}`;
}

describe('parseSse', () => {
  it('emits an event split across two chunks exactly once and complete', () => {
    const raw = block(TOKEN);
    const cut = Math.floor(raw.length / 2);

    const first = parseSse(raw.slice(0, cut), '');
    expect(first.events).toEqual([]);
    expect(first.rest).toBe(raw.slice(0, cut));

    const second = parseSse(raw.slice(cut), first.rest);
    expect(second.events).toEqual([TOKEN]);
    expect(second.rest).toBe('');
  });

  it('emits every event of a chunk in order and keeps the incomplete tail', () => {
    const tail = 'event: token\ndata: {"type":"tok';
    const result = parseSse(block(TOKEN) + block(TOOL_START) + tail, '');

    expect(result.events).toEqual([TOKEN, TOOL_START]);
    expect(result.rest).toBe(tail);
  });

  it('accepts CRLF line endings, also split between chunks', () => {
    const raw = block(TOKEN, '\r\n') + block(TOOL_START, '\r\n');
    const cut = raw.indexOf('\r\n\r\n') + 3;

    const first = parseSse(raw.slice(0, cut), '');
    const second = parseSse(raw.slice(cut), first.rest);

    expect([...first.events, ...second.events]).toEqual([TOKEN, TOOL_START]);
    expect(second.rest).toBe('');
  });

  it('ignores a block with invalid JSON and keeps parsing the next ones', () => {
    const result = parseSse('event: token\ndata: {not json\n\n' + block(TOKEN), '');

    expect(result.events).toEqual([TOKEN]);
  });

  it('ignores comments, blocks without data and blocks whose event name diverges from type', () => {
    const mismatch = `event: done\ndata: ${JSON.stringify(TOKEN)}\n\n`;
    const result = parseSse(': keep-alive\n\nevent: token\n\n' + mismatch + block(TOKEN), '');

    expect(result.events).toEqual([TOKEN]);
  });

  it('accepts the start event', () => {
    const start: StreamEvent = {
      type: 'start',
      conversation_id: '11111111-1111-1111-1111-111111111111',
      trace_id: 'trace-1',
    };
    const result = parseSse(block(start) + block(TOKEN), '');

    expect(result.events).toEqual([start, TOKEN]);
  });

  it('ignores JSON without a known type', () => {
    const result = parseSse('data: {"type":"unknown"}\n\ndata: [1,2]\n\ndata: "x"\n\n', '');

    expect(result.events).toEqual([]);
  });

  it('joins multiple data lines with a newline and accepts a block without an event line', () => {
    const result = parseSse('data: {"type":"token",\ndata: "text":"a"}\n\n', '');

    expect(result.events).toEqual([{ type: 'token', text: 'a' }]);
  });

  it('accepts fields without the space after the colon', () => {
    const result = parseSse(`event:token\ndata:${JSON.stringify(TOKEN)}\n\n`, '');

    expect(result.events).toEqual([TOKEN]);
  });
});
