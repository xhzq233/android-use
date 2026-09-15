package io.github.xhzq233.androiduse;

import android.app.UiAutomation;
import android.os.Process;
import android.view.accessibility.AccessibilityNodeInfo;

import com.android.uiautomator.core.UiDevice;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicBoolean;

public final class UiAutomationServerTest extends UiAutomatorTestCase {
    public static final int PORT = 27183;

    private static final int PROTOCOL_VERSION = 2;
    private static final int IDLE_TIMEOUT_MS = 2 * 60 * 1000;
    private static final int REQUEST_TIMEOUT_MS = 2 * 1000;
    private static final int STARTUP_TIMEOUT_MS = 5 * 1000;
    private static final int DUMP_TIMEOUT_MS = 5 * 1000;
    private static final int MAX_REQUEST_BYTES = 256;
    private static final long MAX_HIERARCHY_BYTES = 32L * 1024 * 1024;
    private static final String DUMPER_CLASS =
            "com.android.uiautomator.core.AccessibilityNodeInfoDumper";
    private static final String HIERARCHY_FILE =
            "/data/local/tmp/android-use-ui-hierarchy.xml";
    private static final String PID_FILE =
            "/data/local/tmp/android-use-ui-server.pid";

    private String buildHash = "-";
    private String sessionToken;

    public void testServe() throws Exception {
        String requestedBuildHash = getParams().getString("androidUseBuild");
        if (requestedBuildHash != null && requestedBuildHash.matches("[0-9a-f]{64}")) {
            buildHash = requestedBuildHash;
        }
        sessionToken = getParams().getString("androidUseToken");
        if (sessionToken == null || !sessionToken.matches("[0-9a-f]{64}")) {
            throw new IllegalArgumentException("androidUseToken must be 64 lowercase hex characters");
        }
        File hierarchyFile = new File(HIERARCHY_FILE);
        File pidFile = new File(PID_FILE);
        ServerSocket server = new ServerSocket();
        AtomicBoolean startupComplete = new AtomicBoolean(false);
        Thread startupWatchdog = processWatchdog(
                startupComplete,
                STARTUP_TIMEOUT_MS,
                "android-use-startup-watchdog"
        );
        try {
            UiDevice device = getUiDevice();
            preserveAccessibilityServices(device);
            server.setReuseAddress(true);
            server.bind(
                    new InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT),
                    1
            );
            server.setSoTimeout(IDLE_TIMEOUT_MS);
            hierarchyFile.delete();
            writePidFile(pidFile);
            startupComplete.set(true);
            startupWatchdog.interrupt();
            boolean running = true;
            while (running) {
                try {
                    Socket client = server.accept();
                    try {
                        client.setSoTimeout(REQUEST_TIMEOUT_MS);
                        running = handleRequest(device, hierarchyFile, client);
                    } finally {
                        client.close();
                    }
                } catch (SocketTimeoutException timeout) {
                    running = false;
                }
            }
        } finally {
            startupComplete.set(true);
            startupWatchdog.interrupt();
            hierarchyFile.delete();
            deletePidFileIfOwned(pidFile);
            server.close();
        }
    }

    private boolean handleRequest(
            UiDevice device,
            File hierarchyFile,
            Socket client
    ) throws IOException {
        OutputStream output = client.getOutputStream();
        String request;
        try {
            request = readLine(client.getInputStream());
        } catch (IOException error) {
            writeError(output, error);
            return true;
        }

        String command = authenticate(request);
        if (command == null) {
            writeAscii(output, "ERR unauthorized\n");
            return true;
        }

        if ("PING".equals(command)) {
            writeAscii(
                    output,
                    "OK " + PROTOCOL_VERSION + " " + Process.myPid()
                            + " " + buildHash + "\n"
            );
            return true;
        }
        if ("DUMP 0".equals(command) || "DUMP 1".equals(command)) {
            AtomicBoolean complete = new AtomicBoolean(false);
            Thread watchdog = processWatchdog(
                    complete,
                    DUMP_TIMEOUT_MS,
                    "android-use-dump-watchdog"
            );
            try {
                device.setCompressedLayoutHeirarchy(command.endsWith("1"));
                device.waitForIdle(500);
                hierarchyFile.delete();
                dumpHierarchy(device, hierarchyFile);
                long hierarchyLength = hierarchyFile.length();
                if (hierarchyLength < 1) {
                    throw new IOException("UI hierarchy file is empty");
                }
                if (hierarchyLength > MAX_HIERARCHY_BYTES) {
                    throw new IOException(
                            "UI hierarchy exceeds " + MAX_HIERARCHY_BYTES + " bytes"
                    );
                }
                writeAscii(
                        output,
                        "OK " + hierarchyLength + " " + PROTOCOL_VERSION
                                + " " + buildHash + "\n"
                );
                streamFile(output, hierarchyFile, hierarchyLength);
            } catch (Exception error) {
                writeError(output, error);
            } finally {
                hierarchyFile.delete();
                complete.set(true);
                watchdog.interrupt();
            }
            return true;
        }
        if ("SHUTDOWN".equals(command)) {
            writeAscii(output, "OK\n");
            return false;
        }

        writeAscii(output, "ERR unsupported request\n");
        return true;
    }

    private String authenticate(String request) {
        String prefix = "AUTH ";
        if (!request.startsWith(prefix)) {
            return null;
        }
        int tokenEnd = request.indexOf(' ', prefix.length());
        if (tokenEnd < 0) {
            return null;
        }
        String suppliedToken = request.substring(prefix.length(), tokenEnd);
        boolean matches = MessageDigest.isEqual(
                suppliedToken.getBytes(StandardCharsets.US_ASCII),
                sessionToken.getBytes(StandardCharsets.US_ASCII)
        );
        return matches ? request.substring(tokenEnd + 1) : null;
    }

    private static Thread processWatchdog(
            final AtomicBoolean complete,
            final int timeoutMillis,
            String name
    ) {
        Thread watchdog = new Thread(
                new Runnable() {
                    @Override
                    public void run() {
                        try {
                            Thread.sleep(timeoutMillis);
                        } catch (InterruptedException finished) {
                            return;
                        }
                        if (!complete.get()) {
                            Process.killProcess(Process.myPid());
                        }
                    }
                },
                name
        );
        watchdog.setDaemon(true);
        watchdog.start();
        return watchdog;
    }

    private void dumpHierarchy(UiDevice device, File hierarchyFile) throws Exception {
        AccessibilityNodeInfo root = rootNode(device);
        if (root == null) {
            throw new IOException("UI hierarchy root is unavailable");
        }
        try {
            Class<?> dumper = Class.forName(DUMPER_CLASS);
            Method dump;
            Object[] arguments;
            try {
                dump = dumper.getMethod(
                        "dumpWindowToFile",
                        AccessibilityNodeInfo.class,
                        File.class,
                        Integer.TYPE,
                        Integer.TYPE,
                        Integer.TYPE
                );
                arguments = new Object[] {
                        root,
                        hierarchyFile,
                        device.getDisplayRotation(),
                        device.getDisplayWidth(),
                        device.getDisplayHeight()
                };
            } catch (NoSuchMethodException ignored) {
                dump = dumper.getMethod(
                        "dumpWindowToFile",
                        AccessibilityNodeInfo.class,
                        File.class
                );
                arguments = new Object[] {root, hierarchyFile};
            }
            try {
                dump.invoke(null, arguments);
            } catch (InvocationTargetException error) {
                Throwable cause = error.getCause();
                if (cause instanceof Exception) {
                    throw (Exception) cause;
                }
                throw error;
            }
        } finally {
            root.recycle();
        }
    }

    private static AccessibilityNodeInfo rootNode(UiDevice device) throws Exception {
        Object bridge = automatorBridge(device);
        Method getRoot = bridge.getClass().getMethod("getRootInActiveWindow");
        return (AccessibilityNodeInfo) getRoot.invoke(bridge);
    }

    private static Object automatorBridge(UiDevice device) throws Exception {
        Method getBridge = UiDevice.class.getDeclaredMethod("getAutomatorBridge");
        getBridge.setAccessible(true);
        return getBridge.invoke(device);
    }

    private static void preserveAccessibilityServices(UiDevice device) throws Exception {
        Object bridge = automatorBridge(device);
        Field automationField = findField(bridge.getClass(), "mUiAutomation");
        automationField.setAccessible(true);
        UiAutomation automation = (UiAutomation) automationField.get(bridge);

        Method disconnect = UiAutomation.class.getDeclaredMethod("disconnect");
        Method connect = UiAutomation.class.getDeclaredMethod("connect", Integer.TYPE);
        Method legacyConnect = UiAutomation.class.getDeclaredMethod("connect");
        disconnect.setAccessible(true);
        connect.setAccessible(true);
        legacyConnect.setAccessible(true);

        disconnect.invoke(automation);
        try {
            connect.invoke(
                    automation,
                    UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES
            );
        } catch (Exception error) {
            try {
                legacyConnect.invoke(automation);
            } catch (Exception reconnectError) {
                error.addSuppressed(reconnectError);
            }
            throw error;
        }
    }

    private static Field findField(Class<?> type, String name) throws NoSuchFieldException {
        Class<?> current = type;
        while (current != null) {
            try {
                return current.getDeclaredField(name);
            } catch (NoSuchFieldException ignored) {
                current = current.getSuperclass();
            }
        }
        throw new NoSuchFieldException(name);
    }

    private static void writePidFile(File pidFile) throws IOException {
        FileWriter writer = new FileWriter(pidFile, false);
        try {
            writer.write(Integer.toString(Process.myPid()));
            writer.write("\n");
        } finally {
            writer.close();
        }
    }

    private static void deletePidFileIfOwned(File pidFile) {
        if (!pidFile.isFile()) {
            return;
        }
        try {
            String value = new String(
                    readSmallFile(pidFile, 64),
                    StandardCharsets.US_ASCII
            ).trim();
            if (value.equals(Integer.toString(Process.myPid()))) {
                pidFile.delete();
            }
        } catch (IOException ignored) {
            // A stale PID file is validated against /proc before host-side cleanup.
        }
    }

    private static String readLine(InputStream input) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        while (bytes.size() < MAX_REQUEST_BYTES) {
            int value = input.read();
            if (value < 0 || value == '\n') {
                return new String(bytes.toByteArray(), StandardCharsets.US_ASCII).trim();
            }
            bytes.write(value);
        }
        throw new IOException("request is too long");
    }

    private static byte[] readSmallFile(File file, int limit) throws IOException {
        if (file.length() > limit) {
            throw new IOException("file is too large");
        }
        FileInputStream input = new FileInputStream(file);
        try {
            ByteArrayOutputStream output = new ByteArrayOutputStream(limit);
            byte[] buffer = new byte[Math.min(limit, 1024)];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                if (output.size() + count > limit) {
                    throw new IOException("file is too large");
                }
                output.write(buffer, 0, count);
            }
            return output.toByteArray();
        } finally {
            input.close();
        }
    }

    private static void streamFile(
            OutputStream output,
            File file,
            long expectedLength
    ) throws IOException {
        FileInputStream input = new FileInputStream(file);
        long written = 0;
        try {
            byte[] buffer = new byte[16 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                written += count;
                if (written > expectedLength) {
                    throw new IOException("UI hierarchy changed while streaming");
                }
                output.write(buffer, 0, count);
            }
        } finally {
            input.close();
        }
        if (written != expectedLength) {
            throw new IOException("UI hierarchy changed while streaming");
        }
        output.flush();
    }

    private static void writeError(OutputStream output, Throwable error) throws IOException {
        String message = error.getClass().getSimpleName() + ": " + error.getMessage();
        writeAscii(output, "ERR " + message.replace('\n', ' ') + "\n");
    }

    private static void writeAscii(OutputStream output, String value) throws IOException {
        output.write(value.getBytes(StandardCharsets.UTF_8));
        output.flush();
    }
}
