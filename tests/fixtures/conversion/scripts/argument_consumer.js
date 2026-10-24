// Synthetic offline fixture: consumes $argument with a protocol that is not
// registered in kelee_config.SCRIPT_ARGUMENT_PROTOCOLS (must block conversion).
const options = $argument;
$done({ body: options });
