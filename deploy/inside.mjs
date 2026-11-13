import { createConnection, createServer } from 'node:net';
import { chmod, rm } from 'node:fs/promises';

for (const [name, host, port] of [
  ['mcp', '127.0.0.1', 25575],
  ['viewer', '127.0.0.1', 3000],
  ['game', 'minecraft', 25565],
]) {
  const path = `/sockets/${name}.sock`;
  await rm(path, { force: true });
  const listener = createServer(client => {
    const upstream = createConnection({ host, port });
    client.on('error', () => upstream.destroy());
    upstream.on('error', () => client.destroy());
    client.on('close', () => upstream.destroy());
    upstream.on('close', () => client.destroy());
    client.pipe(upstream).pipe(client);
  });
  await new Promise((resolve, reject) => listener.once('error', reject).listen(path, resolve));
  await chmod(path, 0o600);
}
const host = Bun.spawn([
  'bun', 'src/server/host.ts', '--minecraft-host', 'minecraft', '--minecraft-port', '25565',
  '--username', 'AI_M1KE', '--version', '1.21.4', '--data-root', '/data',
], { stdout: 'inherit', stderr: 'inherit' });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => host.kill(signal));
process.exit(await host.exited);
