// Synthetic offline fixture: upstream-style response script that only touches
// $response (no $argument); used to prove mirroring/providers still work.
const body = $response.body;
$done({ body: body });
