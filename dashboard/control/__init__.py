"""The controller: what should exist, and how it is made to exist.

Three ideas, and keeping them apart is the point of the package.

`states` is the lifecycle, as a table. It knows nothing about runners, forges
or Docker - only which state may follow which, and what drives each step.

`service` records intent. It never performs work: a call writes what should be
true and returns an operation id. That is what makes every mutating call safe
to replay and every slow change observable while it is happening.

`reconciler` makes intent true, one step at a time, and is the only writer of
`actual_state`. Everything it does is phrased as "make this true" rather than
"do this", so running it twice over a converged fleet does nothing at all.

The split exists because the alternative is what this platform is replacing: a
route handler that shells out to Docker, waits, and returns when it is done -
which cannot be retried, cannot be observed, and leaves half a runner behind
when the request times out.
"""
