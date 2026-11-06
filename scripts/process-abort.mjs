// Each runner starts a process group so cancellation also stops Python's browser descendants.
export function attachAbort(child, signal) {
  if (!signal) return () => {};
  let timer;
  const kill = kind => {
    try { if (process.platform === 'win32') child.kill(kind); else process.kill(-child.pid, kind); } catch { /* Group already exited. */ }
  };
  const abort = () => {
    kill('SIGTERM'); timer = setTimeout(() => kill('SIGKILL'), 5000); timer.unref();
  };
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) abort();
  return () => { signal.removeEventListener('abort', abort); if (!signal.aborted) clearTimeout(timer); };
}
