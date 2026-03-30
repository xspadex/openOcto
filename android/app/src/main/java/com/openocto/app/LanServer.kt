package com.openocto.app

import android.util.Log
import java.io.*
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder

/**
 * Lightweight HTTP server for LAN file transfer.
 * Runs on port 9527, serves files and accepts uploads.
 */
class LanServer(private val port: Int = 9527) {
    private var serverSocket: ServerSocket? = null
    private var running = false
    private val TAG = "OctoLanServer"

    fun start() {
        if (running) return
        running = true
        Thread {
            try {
                serverSocket = ServerSocket(port, 50, InetAddress.getByName("0.0.0.0"))
                Log.i(TAG, "LAN server started on port $port")
                while (running) {
                    try {
                        val socket = serverSocket?.accept() ?: break
                        Thread { handleConnection(socket) }.start()
                    } catch (e: Exception) {
                        if (running) Log.e(TAG, "Accept error: ${e.message}")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Server start failed: ${e.message}")
            }
        }.start()
    }

    fun stop() {
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
    }

    private fun handleConnection(socket: Socket) {
        try {
            socket.soTimeout = 300000 // 5 min timeout
            val input = BufferedInputStream(socket.getInputStream())
            val output = BufferedOutputStream(socket.getOutputStream())

            // Parse HTTP request line
            val requestLine = readLine(input)
            if (requestLine.isNullOrEmpty()) { socket.close(); return }

            val parts = requestLine.split(" ")
            if (parts.size < 3) { socket.close(); return }

            val method = parts[0]
            val path = parts[1]

            // Read headers
            val headers = mutableMapOf<String, String>()
            while (true) {
                val line = readLine(input) ?: break
                if (line.isEmpty()) break
                val colonIdx = line.indexOf(':')
                if (colonIdx > 0) {
                    headers[line.substring(0, colonIdx).trim().lowercase()] =
                        line.substring(colonIdx + 1).trim()
                }
            }

            when {
                method == "GET" && path == "/health" -> {
                    sendResponse(output, 200, "text/plain", "ok".toByteArray())
                }
                method == "GET" && path.startsWith("/file?") -> {
                    handleFileDownload(output, path)
                }
                method == "POST" && path.startsWith("/receive") -> {
                    val contentLength = headers["content-length"]?.toLongOrNull() ?: 0
                    handleFileUpload(output, path, input, contentLength)
                }
                else -> {
                    sendResponse(output, 404, "text/plain", "Not found".toByteArray())
                }
            }

            output.flush()
            socket.close()
        } catch (e: Exception) {
            Log.e(TAG, "Connection error: ${e.message}")
            try { socket.close() } catch (_: Exception) {}
        }
    }

    private fun handleFileDownload(output: OutputStream, path: String) {
        val query = path.substringAfter("?", "")
        val params = parseQuery(query)
        val filePath = params["path"] ?: run {
            sendResponse(output, 400, "text/plain", "Missing path".toByteArray())
            return
        }

        val file = File(filePath)
        if (!file.exists() || !file.isFile) {
            sendResponse(output, 404, "text/plain", "File not found".toByteArray())
            return
        }

        val data = file.readBytes()
        sendResponse(output, 200, "application/octet-stream", data,
            mapOf("X-Filename" to file.name))
    }

    private fun handleFileUpload(output: OutputStream, path: String,
                                  input: InputStream, contentLength: Long) {
        val query = path.substringAfter("?", "")
        val params = parseQuery(query)
        val dest = params["dest"] ?: run {
            sendResponse(output, 400, "text/plain", "Missing dest".toByteArray())
            return
        }

        val file = File(dest)
        file.parentFile?.mkdirs()

        FileOutputStream(file).use { fos ->
            val buffer = ByteArray(65536)
            var remaining = contentLength
            while (remaining > 0) {
                val toRead = minOf(buffer.size.toLong(), remaining).toInt()
                val read = input.read(buffer, 0, toRead)
                if (read <= 0) break
                fos.write(buffer, 0, read)
                remaining -= read
            }
        }

        val response = """{"status":"ok","path":"$dest","size":$contentLength}"""
        sendResponse(output, 200, "application/json", response.toByteArray())
    }

    private fun sendResponse(output: OutputStream, code: Int, contentType: String,
                              body: ByteArray, extraHeaders: Map<String, String> = emptyMap()) {
        val status = when (code) {
            200 -> "OK"; 400 -> "Bad Request"; 404 -> "Not Found"; 500 -> "Error"
            else -> "Unknown"
        }
        val sb = StringBuilder()
        sb.append("HTTP/1.1 $code $status\r\n")
        sb.append("Content-Type: $contentType\r\n")
        sb.append("Content-Length: ${body.size}\r\n")
        extraHeaders.forEach { (k, v) -> sb.append("$k: $v\r\n") }
        sb.append("\r\n")
        output.write(sb.toString().toByteArray())
        output.write(body)
    }

    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val b = input.read()
            if (b == -1) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) return sb.toString().trimEnd('\r')
            sb.append(b.toChar())
        }
    }

    private fun parseQuery(query: String): Map<String, String> {
        if (query.isEmpty()) return emptyMap()
        return query.split("&").mapNotNull {
            val eq = it.indexOf('=')
            if (eq > 0) {
                val key = URLDecoder.decode(it.substring(0, eq), "UTF-8")
                val value = URLDecoder.decode(it.substring(eq + 1), "UTF-8")
                key to value
            } else null
        }.toMap()
    }
}
