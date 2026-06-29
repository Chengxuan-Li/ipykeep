import * as net from 'net';
import { readDescriptor, RuntimeInfo } from './runtimeDescriptor';

export class DaemonError extends Error {}

export interface RunRequest {
  run_id: string | null;
  cells: string[];
}

export interface StaleCell {
  cell_id: string;
  cell_index: number;
  reason: string;
}

/**
 * JSON-RPC client mirroring `ipykeep/client.py:client_call`: newline-delimited
 * JSON over TCP loopback, `{id, token, method, params}` -> `{result | error}`.
 * A fresh connection is opened per call (the daemon handles one request per line).
 */
export class DaemonClient {
  constructor(private notebookPath: string) {}

  private info(): RuntimeInfo {
    const info = readDescriptor(this.notebookPath);
    if (!info) {
      throw new DaemonError(`no ipykeep daemon for ${this.notebookPath}; run \`ipykeep start\` first`);
    }
    return info;
  }

  call<T = any>(method: string, params: any = {}, timeoutMs = 60000): Promise<T> {
    const info = this.info();
    const payload = JSON.stringify({ id: 1, token: info.token, method, params }) + '\n';
    return new Promise<T>((resolve, reject) => {
      const sock = net.createConnection({ host: '127.0.0.1', port: info.port });
      let buf = '';
      let settled = false;
      const done = (fn: () => void) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        try { sock.end(); } catch { /* ignore */ }
        fn();
      };
      const timer = setTimeout(
        () => done(() => reject(new DaemonError(`timeout calling ${method}`))),
        timeoutMs,
      );
      sock.on('connect', () => sock.write(payload));
      sock.on('data', (d) => {
        buf += d.toString('utf8');
        if (!buf.includes('\n')) return;
        done(() => {
          try {
            const resp = JSON.parse(buf);
            if (resp.error) {
              reject(new DaemonError(resp.error.message || 'daemon error'));
            } else {
              resolve(resp.result as T);
            }
          } catch (e) {
            reject(new DaemonError(`bad response from daemon: ${e}`));
          }
        });
      });
      sock.on('error', (e) => done(() => reject(new DaemonError(String(e)))));
    });
  }

  // ----- typed wrappers -----
  status() { return this.call<any>('status', {}, 30000); }
  registerWatcher(clientId: string) {
    return this.call<{ client_id: string }>('register_watcher', { client_id: clientId });
  }
  unregisterWatcher(clientId: string) {
    return this.call('unregister_watcher', { client_id: clientId });
  }
  setCellIdMap(cellMap: Array<{ vscode_uri: string; nbformat_id: string; index: number }>) {
    return this.call('set_cell_id_map', { cell_map: cellMap });
  }
  /** Long-poll; the connection is held open up to `timeoutS` server-side. */
  awaitRunRequest(timeoutS: number) {
    return this.call<RunRequest>('await_run_request', { timeout: timeoutS }, (timeoutS + 20) * 1000);
  }
  reportRunComplete(runId: string, results: any) {
    return this.call('report_run_complete', { run_id: runId, results });
  }
  runStale(execute: boolean) {
    return this.call<any>('run_stale', { execute }, 10 * 60 * 1000);
  }
  getStaleSet() { return this.call<StaleCell[]>('get_stale_set'); }
}
