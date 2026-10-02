#include <errno.h>
#include <limits.h>
#include <node_api.h>
#include <sandbox.h>
#include <unistd.h>

extern char **environ;

static int sealed = 0;

static int seal_process(napi_env env) {
    if (sealed) {
        napi_throw_error(env, "EALREADY", "Maka Host sandbox is already sealed");
        return 0;
    }
    char *error = NULL;
    int result = sandbox_init(
        "(version 1)(allow default)(deny process-fork)(deny process-exec)",
        0, &error);
    if (result != 0) {
        napi_throw_error(env, "ESANDBOX", error == NULL ? "sandbox_init failed" : error);
        sandbox_free_error(error);
        return 0;
    }
    sealed = 1;
    return 1;
}

static napi_value probe_exec(napi_env env, napi_callback_info info) {
    napi_value values[2];
    size_t count = 2;
    char executable[PATH_MAX];
    char marker[PATH_MAX];
    size_t length;
    if (!sealed || napi_get_cb_info(env, info, &count, values, NULL, NULL) != napi_ok ||
        count != 2 ||
        napi_get_value_string_utf8(env, values[0], NULL, 0, &length) != napi_ok ||
        length == 0 || length >= sizeof(executable) ||
        napi_get_value_string_utf8(env, values[0], executable, sizeof(executable), &length) != napi_ok ||
        length == 0 || executable[0] != '/' ||
        napi_get_value_string_utf8(env, values[1], NULL, 0, &length) != napi_ok ||
        length == 0 || length >= sizeof(marker) ||
        napi_get_value_string_utf8(env, values[1], marker, sizeof(marker), &length) != napi_ok ||
        length == 0 || marker[0] != '/') {
        napi_throw_error(env, "EINVAL", "Maka Host native exec probe input is invalid");
        return NULL;
    }
    char *arguments[] = {
        executable,
        "-e",
        "require('node:fs').writeFileSync(process.argv[1], 'launched')",
        marker,
        NULL,
    };
    errno = 0;
    execve(executable, arguments, environ);
    napi_value code;
    napi_create_int32(env, errno, &code);
    return code;
}

static napi_value initialize(napi_env env, napi_value exports) {
    if (!seal_process(env)) {
        return NULL;
    }
    napi_property_descriptor methods[] = {
        {"probeExec", NULL, probe_exec, NULL, NULL, NULL, napi_default, NULL},
    };
    napi_define_properties(env, exports, 1, methods);
    return exports;
}

NAPI_MODULE(NODE_GYP_MODULE_NAME, initialize)
