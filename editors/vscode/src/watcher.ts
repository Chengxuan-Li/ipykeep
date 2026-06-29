import * as fs from 'fs';
import * as vscode from 'vscode';
import { DaemonClient } from './daemonClient';

function genClientId(): string {
  return 'vscode-' + Math.random().toString(16).slice(2, 10);
}

/** nbformat ids of the code cells, in order, read from the .ipynb on disk. */
function diskCodeCellIds(notebookPath: string): string[] {
  try {
    const nb = JSON.parse(fs.readFileSync(notebookPath, 'utf8'));
    const cells: any[] = Array.isArray(nb.cells) ? nb.cells : [];
    return cells
      .filter((c) => c.cell_type === 'code')
      .map((c, i) => (typeof c.id === 'string' && c.id ? c.id : `idx-${i}`));
  } catch {
    return [];
  }
}

function codeCells(notebook: vscode.NotebookDocument): vscode.NotebookCell[] {
  return notebook.getCells().filter((c) => c.kind === vscode.NotebookCellKind.Code);
}

/**
 * Drives delegated execution for one open notebook: registers as the daemon's
 * watcher, keeps the vscode-uri <-> nbformat-id map current, and runs the cells
 * the daemon asks for through VS Code's own pipeline so outputs stream live.
 */
export class NotebookWatcher {
  private clientId = genClientId();
  private running = false;
  private disposables: vscode.Disposable[] = [];

  constructor(
    public readonly notebook: vscode.NotebookDocument,
    private readonly client: DaemonClient,
    private readonly pollSeconds: () => number,
    private readonly log: vscode.OutputChannel,
  ) {}

  get notebookPath(): string {
    return this.notebook.uri.fsPath;
  }

  async start(): Promise<void> {
    const res = await this.client.registerWatcher(this.clientId);
    this.clientId = res.client_id || this.clientId;
    await this.pushCellMap();
    this.running = true;
    this.log.appendLine(`[watcher] attached as ${this.clientId} for ${this.notebook.uri.fsPath}`);

    // Rebuild the map when cells are added/removed/reordered.
    this.disposables.push(
      vscode.workspace.onDidChangeNotebookDocument((e) => {
        if (e.notebook === this.notebook && e.contentChanges.length > 0) {
          void this.pushCellMap();
        }
      }),
    );

    void this.loop();
  }

  private async pushCellMap(): Promise<void> {
    const diskIds = diskCodeCellIds(this.notebookPath);
    const cells = codeCells(this.notebook);
    const map = cells.map((c, i) => ({
      vscode_uri: c.document.uri.toString(),
      nbformat_id: diskIds[i] ?? `idx-${i}`,
      index: i,
    }));
    try {
      await this.client.setCellIdMap(map);
    } catch (e) {
      this.log.appendLine(`[watcher] set_cell_id_map failed: ${e}`);
    }
  }

  private async loop(): Promise<void> {
    while (this.running) {
      let req;
      try {
        req = await this.client.awaitRunRequest(this.pollSeconds());
      } catch (e) {
        if (!this.running) break;
        this.log.appendLine(`[watcher] poll error: ${e}`);
        await new Promise((r) => setTimeout(r, 2000));
        continue;
      }
      if (!req || req.run_id === null) {
        continue; // long-poll timed out with nothing to do; re-poll
      }
      const runId = req.run_id;
      try {
        await this.runCells(req.cells);
        await this.client.reportRunComplete(runId, {
          executed: req.cells,
          outputs: [],
          errors: req.cells.map(() => null),
        });
      } catch (e) {
        this.log.appendLine(`[watcher] run failed: ${e}`);
        await this.client.reportRunComplete(runId, {
          executed: [],
          outputs: [],
          errors: [String(e)],
        });
      }
    }
  }

  private async runCells(nbIds: string[]): Promise<void> {
    if (this.notebook.isDirty) {
      vscode.window.showWarningMessage(
        'ipykeep: the notebook has unsaved edits; skipping the agent-triggered run to avoid ' +
          'clobbering them. Save or revert, then re-run.',
      );
      throw new Error('notebook is dirty');
    }
    const diskIds = diskCodeCellIds(this.notebookPath);
    const cells = codeCells(this.notebook);
    const ranges: Array<{ start: number; end: number }> = [];
    for (const id of nbIds) {
      const codeIdx = diskIds.indexOf(id);
      if (codeIdx < 0 || codeIdx >= cells.length) {
        continue;
      }
      const absIdx = cells[codeIdx].index; // absolute index incl. markdown cells
      ranges.push({ start: absIdx, end: absIdx + 1 });
    }
    if (ranges.length === 0) {
      return;
    }
    // VS Code issues the execute_request, so outputs render in the cells live.
    await vscode.commands.executeCommand('notebook.cell.execute', {
      ranges,
      document: this.notebook.uri,
    });
    // Save so the notebook returns to a clean state; otherwise the unsaved outputs
    // keep it dirty and VS Code won't auto-reload the agent's next on-disk edit.
    try {
      await this.notebook.save();
    } catch (e) {
      this.log.appendLine(`[watcher] save after run failed: ${e}`);
    }
  }

  async dispose(): Promise<void> {
    this.running = false;
    for (const d of this.disposables) {
      try { d.dispose(); } catch { /* ignore */ }
    }
    this.disposables = [];
    try {
      await this.client.unregisterWatcher(this.clientId);
    } catch {
      /* daemon may already be gone */
    }
    this.log.appendLine('[watcher] detached');
  }
}
