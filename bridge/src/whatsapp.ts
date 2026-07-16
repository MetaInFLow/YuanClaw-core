/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadMediaMessage,
  extractMessageContent as baileysExtractMessageContent,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { writeFile, mkdir, readdir, stat, unlink } from 'fs/promises';
import { join } from 'path';
import { randomBytes } from 'crypto';

const VERSION = '0.1.0';
const MAX_MEDIA_BYTES = 25 * 1024 * 1024;
const MAX_MEDIA_FILES = 1000;
const MEDIA_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  participant?: string;
  participantPn?: string;
  media?: string[];
}

export interface InboundMediaDescriptor {
  fallbackContent: string;
  mimetype?: string;
  fileName?: string;
  declaredBytes?: number;
}

function declaredByteLength(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'bigint') return Number(value);
  if (value && typeof (value as { toNumber?: unknown }).toNumber === 'function') {
    const converted = (value as { toNumber: () => number }).toNumber();
    return Number.isFinite(converted) ? converted : undefined;
  }
  return undefined;
}

export function describeInboundMedia(message: any): InboundMediaDescriptor | null {
  const candidates = [
    ['imageMessage', '[Image]'],
    ['documentMessage', '[Document]'],
    ['videoMessage', '[Video]'],
    ['audioMessage', '[Voice Message]'],
  ] as const;
  for (const [field, fallbackContent] of candidates) {
    const media = message?.[field];
    if (!media) continue;
    return {
      fallbackContent,
      mimetype: media.mimetype ?? undefined,
      fileName: field === 'documentMessage' ? media.fileName ?? undefined : undefined,
      declaredBytes: declaredByteLength(media.fileLength),
    };
  }
  return null;
}

export function inboundParticipant(key: any, isGroup: boolean): {
  participant?: string;
  participantPn?: string;
} {
  if (!isGroup) return {};
  return {
    ...(key?.participant ? { participant: String(key.participant) } : {}),
    ...(key?.participantAlt ? { participantPn: String(key.participantAlt) } : {}),
  };
}

export async function pruneMediaDirectory(
  mediaDir: string,
  preservePath: string,
  maxFiles = MAX_MEDIA_FILES,
  maxAgeMs = MEDIA_RETENTION_MS,
  now = Date.now(),
): Promise<void> {
  const entries = await readdir(mediaDir, { withFileTypes: true });
  const retained: Array<{ path: string; mtimeMs: number }> = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.startsWith('wa_')) continue;
    const path = join(mediaDir, entry.name);
    const info = await stat(path);
    if (path !== preservePath && now - info.mtimeMs > maxAgeMs) {
      await unlink(path);
      continue;
    }
    retained.push({ path, mtimeMs: info.mtimeMs });
  }
  retained.sort((left, right) => right.mtimeMs - left.mtimeMs);
  const keep = new Set<string>([preservePath]);
  for (const item of retained) {
    if (keep.size >= Math.max(1, maxFiles)) break;
    keep.add(item.path);
  }
  for (const item of retained) {
    if (!keep.has(item.path)) await unlink(item.path);
  }
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  async connect(): Promise<void> {
    if (this.stopped) return;
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['yuanclaw', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect) this.scheduleReconnect();
      } else if (connection === 'open') {
        this.reconnecting = false;
        if (this.reconnectTimer) {
          clearTimeout(this.reconnectTimer);
          this.reconnectTimer = null;
        }
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        if (msg.key.fromMe) continue;
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const unwrapped = baileysExtractMessageContent(msg.message);
        if (!unwrapped) continue;

        const content = this.getTextContent(unwrapped);
        const mediaPaths: string[] = [];
        const inboundMedia = describeInboundMedia(unwrapped);
        if (inboundMedia) {
          const path = await this.downloadMedia(
            msg,
            inboundMedia.mimetype,
            inboundMedia.fileName,
            inboundMedia.declaredBytes,
          );
          if (path) mediaPaths.push(path);
        }

        const finalContent = content
          || (mediaPaths.length === 0 ? inboundMedia?.fallbackContent : '')
          || '';
        if (!finalContent && mediaPaths.length === 0) continue;

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;

        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: finalContent,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          ...inboundParticipant(msg.key, isGroup),
          ...(mediaPaths.length > 0 ? { media: mediaPaths } : {}),
        });
      }
    });
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnecting || this.reconnectTimer) return;
    this.reconnecting = true;
    console.log('Reconnecting in 5 seconds...');
    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null;
      this.reconnecting = false;
      if (this.stopped) return;
      try {
        await this.connect();
      } catch (error) {
        console.error('WhatsApp reconnect failed:', error);
        this.scheduleReconnect();
      }
    }, 5000);
  }

  private async downloadMedia(
    msg: any,
    mimetype?: string,
    fileName?: string,
    declaredBytes?: number,
  ): Promise<string | null> {
    try {
      if (declaredBytes !== undefined && declaredBytes > MAX_MEDIA_BYTES) {
        console.error('Media download rejected: declared size exceeds limit');
        return null;
      }
      const mediaDir = join(this.options.authDir, '..', 'media');
      await mkdir(mediaDir, { recursive: true });

      const buffer = await downloadMediaMessage(msg, 'buffer', {}) as Buffer;
      if (buffer.byteLength > MAX_MEDIA_BYTES) {
        console.error('Media download rejected: downloaded size exceeds limit');
        return null;
      }

      let outFilename: string;
      if (fileName) {
        // Documents have a filename — use it with a unique prefix to avoid collisions
        const prefix = `wa_${Date.now()}_${randomBytes(4).toString('hex')}_`;
        outFilename = prefix + fileName;
      } else {
        const mime = mimetype || 'application/octet-stream';
        // Derive extension from mimetype subtype (e.g. "image/png" → ".png", "application/pdf" → ".pdf")
        const ext = '.' + (mime.split('/').pop()?.split(';')[0] || 'bin');
        outFilename = `wa_${Date.now()}_${randomBytes(4).toString('hex')}${ext}`;
      }

      const filepath = join(mediaDir, outFilename);
      await writeFile(filepath, buffer);
      try {
        await pruneMediaDirectory(mediaDir, filepath);
      } catch (error) {
        console.error('Failed to prune old media:', error);
      }

      return filepath;
    } catch (err) {
      console.error('Failed to download media:', err);
      return null;
    }
  }

  private getTextContent(message: any): string | null {
    // Text message
    if (message.conversation) {
      return message.conversation;
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return message.extendedTextMessage.text;
    }

    // Image with optional caption
    if (message.imageMessage) {
      return message.imageMessage.caption || '';
    }

    // Video with optional caption
    if (message.videoMessage) {
      return message.videoMessage.caption || '';
    }

    // Document with optional caption
    if (message.documentMessage) {
      return message.documentMessage.caption || '';
    }

    // Voice/Audio message
    if (message.audioMessage) {
      return `[Voice Message]`;
    }

    return null;
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    await this.sock.sendMessage(to, { text });
  }

  async disconnect(): Promise<void> {
    this.stopped = true;
    this.reconnecting = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
