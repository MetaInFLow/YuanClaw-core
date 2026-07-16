import assert from 'node:assert/strict';
import { mkdtemp, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
  describeInboundMedia,
  inboundParticipant,
  pruneMediaDirectory,
} from './whatsapp.js';

test('inboundParticipant preserves the real group participant', () => {
  assert.deepEqual(
    inboundParticipant(
      { participant: 'member@lid', participantAlt: '15550001111@s.whatsapp.net' },
      true,
    ),
    { participant: 'member@lid', participantPn: '15550001111@s.whatsapp.net' },
  );
  assert.deepEqual(inboundParticipant({ participant: 'member@lid' }, false), {});
});

test('describeInboundMedia includes voice messages and declared size', () => {
  assert.deepEqual(
    describeInboundMedia({
      audioMessage: { mimetype: 'audio/ogg; codecs=opus', fileLength: 1234 },
    }),
    {
      fallbackContent: '[Voice Message]',
      mimetype: 'audio/ogg; codecs=opus',
      fileName: undefined,
      declaredBytes: 1234,
    },
  );
});

test('pruneMediaDirectory removes expired and excess bridge media', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'yuanclaw-wa-media-'));
  const current = join(dir, 'wa_current.ogg');
  const extra = join(dir, 'wa_extra.png');
  const expired = join(dir, 'wa_expired.pdf');
  await Promise.all([
    writeFile(current, 'current'),
    writeFile(extra, 'extra'),
    writeFile(expired, 'expired'),
  ]);
  await utimes(expired, new Date(0), new Date(0));

  await pruneMediaDirectory(dir, current, 1, 1000, Date.now());

  const remaining = await import('node:fs/promises').then(({ readdir }) => readdir(dir));
  assert.deepEqual(remaining, ['wa_current.ogg']);
});
