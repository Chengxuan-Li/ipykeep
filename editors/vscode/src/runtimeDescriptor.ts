import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { projectHash } from './projectHash';

export interface RuntimeInfo {
  pid: number;
  port: number;
  token: string;
  notebook: string;
  server_pid?: number | null;
  started_at?: number;
}

/**
 * Runtime descriptor directory, matching `daemon/pid.py:runtime_dir`:
 * `$XDG_RUNTIME_DIR/ipykeep` or `<os tmpdir>/ipykeep`.
 */
export function runtimeDir(): string {
  const base = process.env.XDG_RUNTIME_DIR || os.tmpdir();
  return path.join(base, 'ipykeep');
}

/** Read the daemon's {port, token, pid, ...} descriptor for a notebook, if any. */
export function readDescriptor(notebookPath: string): RuntimeInfo | undefined {
  const p = path.join(runtimeDir(), `${projectHash(notebookPath)}.json`);
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8')) as RuntimeInfo;
  } catch {
    return undefined;
  }
}
