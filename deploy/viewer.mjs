import startViewer from 'prismarine-viewer/lib/mineflayer.js';

export function attachViewer(bot) {
  startViewer(bot, { firstPerson: true, port: 3000, viewDistance: 4 });
  return () => bot.viewer?.close();
}
