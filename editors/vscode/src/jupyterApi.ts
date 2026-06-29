import * as vscode from 'vscode';

/**
 * Minimal shape of the ms-toolsai.jupyter extension API used here. Declared
 * locally to avoid a build-time dependency on @vscode/jupyter-extension.
 *
 * NOTE (dev-host validation point): the exact `createJupyterServerCollection`
 * provider contract has evolved across Jupyter-extension versions. This module
 * is the first thing to confirm in an Extension Development Host. If the shape
 * differs, `registerWarmServer` degrades gracefully (logs + returns undefined)
 * and the caller falls back to copying the server URL to the clipboard.
 */
interface JupyterServerConnectionInformation {
  baseUrl: vscode.Uri;
  token?: string;
}
interface JupyterServer {
  id: string;
  label: string;
  connectionInformation?: JupyterServerConnectionInformation;
}
interface JupyterServerProvider {
  provideJupyterServers(token: vscode.CancellationToken): Promise<JupyterServer[]> | JupyterServer[];
  resolveJupyterServer(server: JupyterServer, token: vscode.CancellationToken):
    Promise<JupyterServer> | JupyterServer;
}
export interface JupyterServerCollection extends vscode.Disposable {
  label: string;
}
interface JupyterAPI {
  createJupyterServerCollection(
    id: string,
    label: string,
    provider: JupyterServerProvider,
  ): JupyterServerCollection;
}

export async function getJupyterApi(): Promise<JupyterAPI | undefined> {
  const ext = vscode.extensions.getExtension('ms-toolsai.jupyter');
  if (!ext) {
    return undefined;
  }
  if (!ext.isActive) {
    await ext.activate();
  }
  return ext.exports as JupyterAPI;
}

/**
 * Register ipykeep's hosted Jupyter server so it appears directly in VS Code's
 * kernel picker (no URL pasting). Returns the collection (dispose to remove) or
 * undefined if the API is unavailable / shaped differently.
 */
export function registerWarmServer(
  api: JupyterAPI,
  collectionId: string,
  label: string,
  baseUrl: string,
  token: string,
): JupyterServerCollection | undefined {
  const server: JupyterServer = {
    id: collectionId,
    label,
    connectionInformation: { baseUrl: vscode.Uri.parse(baseUrl), token },
  };
  try {
    return api.createJupyterServerCollection(collectionId, label, {
      provideJupyterServers: () => [server],
      resolveJupyterServer: (s) => s,
    });
  } catch (e) {
    console.error('ipykeep: createJupyterServerCollection failed', e);
    return undefined;
  }
}

/** Open the notebook document and reveal it in an editor. */
export async function openNotebook(uri: vscode.Uri): Promise<vscode.NotebookEditor> {
  const doc = await vscode.workspace.openNotebookDocument(uri);
  return vscode.window.showNotebookDocument(doc);
}

/**
 * Best-effort kernel selection. Programmatic remote-kernel selection has no
 * stable public API, so we open the (now pre-populated) kernel picker and let
 * the user confirm "ipykeep" in one click. Validate in a dev host whether a
 * fully automatic selection is achievable for the installed Jupyter version.
 */
export async function promptSelectKernel(): Promise<void> {
  try {
    await vscode.commands.executeCommand('notebook.selectKernel');
  } catch (e) {
    console.error('ipykeep: notebook.selectKernel failed', e);
  }
}
