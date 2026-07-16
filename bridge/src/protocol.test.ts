import assert from 'node:assert/strict';
import test from 'node:test';

import { parseSendCommand } from './protocol.js';

test('parseSendCommand accepts and normalizes a send command', () => {
  assert.deepEqual(
    parseSendCommand({ type: 'send', to: ' 123@s.whatsapp.net ', text: 'hello' }),
    { type: 'send', to: '123@s.whatsapp.net', text: 'hello' },
  );
});

test('parseSendCommand rejects unsupported and malformed commands', () => {
  const invalid = [
    null,
    [],
    { type: 'ping' },
    { type: 'send', to: '', text: 'hello' },
    { type: 'send', to: 'recipient', text: '' },
    { type: 'send', to: 'x'.repeat(513), text: 'hello' },
    { type: 'send', to: 'recipient', text: 'x'.repeat(65_537) },
  ];

  for (const command of invalid) {
    assert.throws(() => parseSendCommand(command), TypeError);
  }
});
