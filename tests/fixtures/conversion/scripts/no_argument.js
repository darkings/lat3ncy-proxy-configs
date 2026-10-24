// Synthetic offline fixture: a script that never reads the argument API, so a
// declared Loon parameter must be dropped with an intentional-ignore diagnostic.
const body = $response.body;
$done({ body: body });
