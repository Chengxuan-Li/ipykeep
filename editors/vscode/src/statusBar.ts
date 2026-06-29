import * as vscode from 'vscode';
import { DaemonClient } from './daemonClient';

/**
 * A status-bar item showing the stale-cell count for the attached notebook, with
 * a click that triggers a delegated run. Per-cell background decorations need the
 * proposed notebook-decoration API; the status bar is the stable, install-anywhere
 * surface and is enough to make staleness visible.
 */
export class StaleStatusBar {
  private item: vscode.StatusBarItem;
  private timer?: NodeJS.Timeout;

  constructor(
    private readonly client: DaemonClient,
    private readonly refreshSeconds: () => number,
  ) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.item.command = 'ipykeep.runStale';
  }

  start(): void {
    this.tick();
    this.timer = setInterval(() => this.tick(), Math.max(1, this.refreshSeconds()) * 1000);
  }

  private async tick(): Promise<void> {
    try {
      const stale = await this.client.getStaleSet();
      const n = stale.length;
      if (n === 0) {
        this.item.text = '$(check) ipykeep: warm';
        this.item.tooltip = 'ipykeep — kernel up to date';
      } else {
        this.item.text = `$(sync) ipykeep: ${n} stale`;
        this.item.tooltip = `ipykeep — ${n} stale cell(s); click to run them on the warm kernel`;
      }
      this.item.show();
    } catch {
      this.item.text = '$(warning) ipykeep: detached';
      this.item.tooltip = 'ipykeep daemon not reachable';
      this.item.show();
    }
  }

  dispose(): void {
    if (this.timer) {
      clearInterval(this.timer);
    }
    this.item.dispose();
  }
}
