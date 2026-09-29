import Link from "next/link";

export default function NotFound() {
  return (
    <div className="card mx-auto max-w-md p-10 text-center">
      <p className="font-mono text-sm text-amber">404</p>
      <h1 className="mt-2 text-xl font-semibold tracking-tight">That page does not exist</h1>
      <p className="mt-2 text-sm text-muted">
        If you were looking for a run, it may have been deleted — or the link is wrong.
      </p>
      <div className="mt-6 flex justify-center gap-2">
        <Link href="/" className="btn-primary">Back home</Link>
        <Link href="/history" className="btn-ghost">Run history</Link>
      </div>
    </div>
  );
}
