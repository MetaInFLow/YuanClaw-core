export interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

const MAX_RECIPIENT_CHARS = 512;
const MAX_TEXT_CHARS = 65_536;

export function parseSendCommand(value: unknown): SendCommand {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('Command must be a JSON object');
  }

  const command = value as Record<string, unknown>;
  if (command.type !== 'send') {
    throw new TypeError('Unsupported command type');
  }
  if (typeof command.to !== 'string' || !command.to.trim()) {
    throw new TypeError('Command recipient must be a non-empty string');
  }
  if (command.to.length > MAX_RECIPIENT_CHARS) {
    throw new TypeError('Command recipient is too long');
  }
  if (typeof command.text !== 'string' || !command.text.trim()) {
    throw new TypeError('Command text must be a non-empty string');
  }
  if (command.text.length > MAX_TEXT_CHARS) {
    throw new TypeError('Command text is too long');
  }

  return {
    type: 'send',
    to: command.to.trim(),
    text: command.text,
  };
}
