import * as vscode from 'vscode';
import { DaemonClient } from './daemonClient';
import { getJupyterApi, JupyterServerCollection, openNotebook, promptSelectKernel, registerWarmServer }
  from './jupyterApi';
import { projectHash } from './projectHash';
import { StaleStatusBar } from './statusBar';
import { NotebookWatcher } from './watcher';

let log: vscode.OutputChannel;

/** One live attachment: server registration + watcher + status bar for a notebook. */
class Attachment {
  watcher?: NotebookWatcher;
  statusBar?: StaleStatusBar;
  collection?: JupyterServerCollection;
  constructor(public readonly path: string) {}

  async dispose(): Promise<void> {
    this.statusBar?.dispose();
    await this.watcher?.dispose();
    this.collection?.dispose();
  }
}

const attachments = new Map<string, Attachment>(); // notebook fsPath -> attachment

function config<T>(key: string, fallback: T): T {
  return vscode.workspace.getConfiguration('ipykeep').get<T>(key, fallback);
}

async function attach(notebookUri: vscode.Uri): Promise<void> {
  const path = notebookUri.fsPath;
  if (attachments.has(path)) {
    await openNotebook(notebookUri);
    return;
  }
  const client = new DaemonClient(path);

  let status: any;
  try {
    status = await client.status();
  } catch (e) {
    vscode.window.showErrorMessage(
      `ipykeep: no warm daemon for ${path}. Start one with \`ipykeep start "${path}" --serve\` ` +
        `(or \`ipykeep open\`). (${e})`,
    );
    return;
  }
  if (status.warming) {
    vscode.window.showInformationMessage('ipykeep: kernel still warming — try again in a moment.');
    return;
  }

  const att = new Attachment(path);
  attachments.set(path, att);

  // 1) Register the warm server so it shows up in the kernel picker.
  const api = await getJupyterApi();
  if (api && status.server_url && status.server_token) {
    // server_url is the human landing URL (…/lab?token=…); the API wants the base.
    const baseUrl = String(status.server_url).split('?')[0].replace(/\/(lab|tree)\/?$/, '/');
    att.collection = registerWarmServer(
      api,
      `ipykeep:${projectHash(path)}`,
      `ipykeep: ${vscode.workspace.asRelativePath(notebookUri)}`,
      baseUrl,
      String(status.server_token),
    );
  } else if (!status.server_url) {
    vscode.window.showWarningMessage(
      'ipykeep: daemon is not hosting a Jupyter server; restart it with `--serve` for one-click ' +
        'kernel attach. Falling back to live staleness only.',
    );
  }

  // 2) Open the notebook and help the user land on the warm kernel.
  await openNotebook(notebookUri);
  if (att.collection) {
    await promptSelectKernel();
  }

  // 3) Start the delegated-execution watcher + stale status bar.
  const doc = vscode.workspace.notebookDocuments.find((d) => d.uri.fsPath === path);
  if (doc) {
    att.watcher = new NotebookWatcher(doc, client, () => config('pollIntervalSeconds', 30), log);
    await att.watcher.start();
  }
  att.statusBar = new StaleStatusBar(client, () => config('staleRefreshSeconds', 4));
  att.statusBar.start();

  log.appendLine(`[ipykeep] attached ${path}`);
}

async function detach(path: string): Promise<void> {
  const att = attachments.get(path);
  if (att) {
    attachments.delete(path);
    await att.dispose();
    log.appendLine(`[ipykeep] detached ${path}`);
  }
}

function activeNotebookPath(): string | undefined {
  return vscode.window.activeNotebookEditor?.notebook.uri.fsPath;
}

export function activate(context: vscode.ExtensionContext): void {
  log = vscode.window.createOutputChannel('ipykeep');
  context.subscriptions.push(log);

  context.subscriptions.push(
    vscode.commands.registerCommand('ipykeep.openLiveNotebook', async () => {
      const active = vscode.window.activeNotebookEditor?.notebook.uri;
      const uri =
        active ||
        (await vscode.window.showOpenDialog({
          canSelectMany: false,
          filters: { Notebooks: ['ipynb'] },
        }))?.[0];
      if (uri) {
        await attach(uri);
      }
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ipykeep.runStale', async () => {
      const path = activeNotebookPath();
      if (!path) {
        return;
      }
      try {
        await new DaemonClient(path).runStale(true);
      } catch (e) {
        vscode.window.showErrorMessage(`ipykeep: run-stale failed: ${e}`);
      }
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand('ipykeep.detach', async () => {
      const path = activeNotebookPath();
      if (path) {
        await detach(path);
      }
    }),
  );

  // Deep link: vscode://ipykeep.ipykeep-vscode/open?notebook=<abs path>
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      handleUri: async (uri: vscode.Uri) => {
        if (uri.path !== '/open') {
          return;
        }
        const params = new URLSearchParams(uri.query);
        const nb = params.get('notebook');
        if (nb) {
          await attach(vscode.Uri.file(nb));
        }
      },
    }),
  );

  // Clean up when a watched notebook is closed.
  context.subscriptions.push(
    vscode.workspace.onDidCloseNotebookDocument((doc) => {
      void detach(doc.uri.fsPath);
    }),
  );

  context.subscriptions.push({
    dispose: () => {
      for (const path of Array.from(attachments.keys())) {
        void detach(path);
      }
    },
  });
}

export function deactivate(): void {
  for (const path of Array.from(attachments.keys())) {
    void detach(path);
  }
}
