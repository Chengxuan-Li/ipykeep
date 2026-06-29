import * as crypto from 'crypto';
import * as path from 'path';

/**
 * Compute the per-notebook descriptor hash, matching ipykeep's
 * `daemon/pid.py:project_hash`: sha1 of the resolved absolute path
 * (lower-cased on Windows), first 16 hex chars.
 *
 * Note: Python's `Path.resolve()` also resolves symlinks; `path.resolve` does
 * not. For ordinary (non-symlinked) notebook paths the results match.
 */
export function projectHash(notebookPath: string): string {
  let abspath = path.resolve(notebookPath);
  if (process.platform === 'win32') {
    abspath = abspath.toLowerCase();
  }
  return crypto.createHash('sha1').update(abspath, 'utf8').digest('hex').slice(0, 16);
}
