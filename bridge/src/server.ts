/**
 * WebSocket server for Python-Node.js bridge communication.
 * Security: binds to 127.0.0.1 only; optional BRIDGE_TOKEN auth.
 */

import { WebSocketServer, WebSocket } from 'ws';
import { WhatsAppClient, InboundMessage } from './whatsapp.js';
import { parseSendCommand, SendCommand } from './protocol.js';

interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error' | 'sent';
  [key: string]: unknown;
}

export class BridgeServer {
  private static readonly MAX_CLIENT_BUFFER_BYTES = 1_048_576;
  private static readonly MAX_COMMAND_BYTES = 1_048_576;

  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private clients: Set<WebSocket> = new Set();

  constructor(private port: number, private authDir: string, private token?: string) {}

  async start(): Promise<void> {
    // Bind to localhost only — never expose to external network
    this.wss = new WebSocketServer({
      host: '127.0.0.1',
      port: this.port,
      maxPayload: BridgeServer.MAX_COMMAND_BYTES,
    });
    console.log(`🌉 Bridge server listening on ws://127.0.0.1:${this.port}`);
    if (this.token) console.log('🔒 Token authentication enabled');

    // Initialize WhatsApp client
    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: (msg) => this.broadcast({ type: 'message', ...msg }),
      onQR: (qr) => this.broadcast({ type: 'qr', qr }),
      onStatus: (status) => this.broadcast({ type: 'status', status }),
    });

    // Handle WebSocket connections
    this.wss.on('connection', (ws) => {
      if (this.token) {
        // Require auth handshake as first message
        const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
        ws.once('message', (data) => {
          clearTimeout(timeout);
          try {
            const msg = JSON.parse(data.toString());
            if (msg.type === 'auth' && msg.token === this.token) {
              console.log('🔗 Python client authenticated');
              this.setupClient(ws);
            } else {
              ws.close(4003, 'Invalid token');
            }
          } catch {
            ws.close(4003, 'Invalid auth message');
          }
        });
      } else {
        console.log('🔗 Python client connected');
        this.setupClient(ws);
      }
    });

    // Connect to WhatsApp
    await this.wa.connect();
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);

    ws.on('message', async (data) => {
      try {
        const cmd = parseSendCommand(JSON.parse(data.toString()));
        await this.handleCommand(cmd);
        this.sendToClient(ws, { type: 'sent', to: cmd.to });
      } catch (error) {
        console.error('Error handling command:', error);
        this.sendToClient(ws, { type: 'error', error: String(error) });
      }
    });

    ws.on('close', () => {
      console.log('🔌 Python client disconnected');
      this.clients.delete(ws);
    });

    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.clients.delete(ws);
    });
  }

  private async handleCommand(cmd: SendCommand): Promise<void> {
    if (!this.wa) {
      throw new Error('WhatsApp client is not available');
    }
    await this.wa.sendMessage(cmd.to, cmd.text);
  }

  private sendToClient(client: WebSocket, msg: BridgeMessage): void {
    if (client.readyState !== WebSocket.OPEN) return;
    if (client.bufferedAmount > BridgeServer.MAX_CLIENT_BUFFER_BYTES) {
      this.clients.delete(client);
      client.close(1013, 'Client is not consuming messages');
      return;
    }

    client.send(JSON.stringify(msg), (error) => {
      if (!error) return;
      console.error('WebSocket send error:', error);
      this.clients.delete(client);
      client.terminate();
    });
  }

  private broadcast(msg: BridgeMessage): void {
    for (const client of this.clients) {
      this.sendToClient(client, msg);
    }
  }

  async stop(): Promise<void> {
    // Close all client connections
    for (const client of this.clients) {
      client.terminate();
    }
    this.clients.clear();

    // Close WebSocket server
    if (this.wss) {
      const server = this.wss;
      this.wss = null;
      await new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) reject(error);
          else resolve();
        });
      });
    }

    // Disconnect WhatsApp
    if (this.wa) {
      await this.wa.disconnect();
      this.wa = null;
    }
  }
}
