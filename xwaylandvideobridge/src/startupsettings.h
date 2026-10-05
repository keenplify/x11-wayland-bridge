// SPDX-License-Identifier: LicenseRef-KDE-Accepted-GPL
#pragma once

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QSaveFile>
#include <QSettings>
#include <QStandardPaths>

namespace StartupSettings
{
inline QString userEntry()
{
    return QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation)
        + QStringLiteral("/autostart/org.kde.xwaylandvideobridge.desktop");
}

inline bool enabled()
{
    const auto path = QStandardPaths::locate(QStandardPaths::GenericConfigLocation,
                                            QStringLiteral("autostart/org.kde.xwaylandvideobridge.desktop"));
    if (path.isEmpty()) {
        return false;
    }
    QSettings entry(path, QSettings::IniFormat);
    entry.beginGroup(QStringLiteral("Desktop Entry"));
    return !entry.value(QStringLiteral("Hidden"), false).toBool()
        && entry.value(QStringLiteral("X-GNOME-Autostart-enabled"), true).toBool();
}

inline bool setEnabled(bool enabled)
{
    const QString path = userEntry();
    QString contents;
    if (QFile::exists(path)) {
        QFile existing(path);
        if (!existing.open(QIODevice::ReadOnly)) {
            return false;
        }
        contents = QString::fromUtf8(existing.readAll());
        if (existing.error() != QFile::NoError) {
            return false;
        }
    } else {
        QString executable = QCoreApplication::applicationFilePath();
        executable.replace(QLatin1Char('\\'), QStringLiteral("\\\\"));
        executable.replace(QLatin1Char('"'), QStringLiteral("\\\""));
        executable.replace(QLatin1Char('`'), QStringLiteral("\\`"));
        executable.replace(QLatin1Char('$'), QStringLiteral("\\$"));
        executable.replace(QLatin1Char('%'), QStringLiteral("%%"));
        contents = QStringLiteral("[Desktop Entry]\nType=Application\nName=Xwayland Video Bridge\n"
                                  "Exec=\"%1\" --autostart\nIcon=org.kde.xwaylandvideobridge\n"
                                  "StartupNotify=false\nX-KDE-autostart-phase=2\n").arg(executable);
    }

    // Preserve the current launcher (including a systemd service) and other groups.
    QStringList result;
    bool inDesktopEntry = false;
    bool foundDesktopEntry = false;
    auto appendFlags = [&] {
        result << QStringLiteral("Hidden=%1").arg(enabled ? QStringLiteral("false") : QStringLiteral("true"))
               << QStringLiteral("X-GNOME-Autostart-enabled=%1").arg(enabled ? QStringLiteral("true") : QStringLiteral("false"));
    };
    for (const QString &line : contents.split(QLatin1Char('\n'))) {
        const QString trimmed = line.trimmed();
        if (trimmed.startsWith(QLatin1Char('[')) && trimmed.endsWith(QLatin1Char(']'))) {
            if (inDesktopEntry) {
                appendFlags();
            }
            inDesktopEntry = trimmed == QLatin1String("[Desktop Entry]");
            foundDesktopEntry |= inDesktopEntry;
        }
        if (inDesktopEntry && (trimmed.section(QLatin1Char('='), 0, 0).trimmed() == QLatin1String("Hidden")
                               || trimmed.section(QLatin1Char('='), 0, 0).trimmed() == QLatin1String("X-GNOME-Autostart-enabled"))) {
            continue;
        }
        result << line;
    }
    if (!foundDesktopEntry) {
        return false;
    }
    if (inDesktopEntry) {
        appendFlags();
    }
    if (!QDir().mkpath(QFileInfo(path).absolutePath())) {
        return false;
    }
    QSaveFile entry(path);
    const QByteArray data = (result.join(QLatin1Char('\n')) + QLatin1Char('\n')).toUtf8();
    return entry.open(QIODevice::WriteOnly) && entry.write(data) == data.size() && entry.commit();
}
}
