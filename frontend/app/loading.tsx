export default function Loading() {
  return (
    <div className="animate-pulse2 space-y-5" aria-busy="true" aria-label="Loading">
      <div className="h-6 w-48 rounded-lg bg-line" />
      <div className="h-20 w-full rounded-2xl bg-line" />
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="h-16 rounded-2xl bg-line" />
          ))}
        </div>
        <div className="h-80 rounded-2xl bg-line" />
      </div>
    </div>
  );
}
