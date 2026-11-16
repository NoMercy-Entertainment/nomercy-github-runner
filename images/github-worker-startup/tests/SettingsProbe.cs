using System;
using System.IO;
using System.Reflection;
using System.Runtime.Loader;

var directory = Path.GetFullPath(args[0]);
AssemblyLoadContext.Default.Resolving += (_, name) =>
    AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.Combine(directory, name.Name + ".dll"));
var assembly = Assembly.LoadFrom(Path.Combine(directory, "Runner.Worker.dll"));
var workerType = assembly.GetType("GitHub.Runner.Worker.Worker", throwOnError: true)!;
var timeout = workerType.GetField("_workerStartTimeout", BindingFlags.Instance | BindingFlags.NonPublic)!;
var completed = workerType.GetField("_completedCommand", BindingFlags.Instance | BindingFlags.NonPublic)!;
const string key = "GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT";
var previous = Environment.GetEnvironmentVariable(key);
try
{
    foreach (var (setting, expected) in new (string?, double)[] {
        (null, 30), ("invalid", 30), ("2147483648", 30), ("-1", 30),
        ("0", 30), ("30", 30), ("120", 120), ("300", 300), ("900", 300) })
    {
        Environment.SetEnvironmentVariable(key, setting);
        var worker = Activator.CreateInstance(workerType)!;
        try
        {
            var actual = ((TimeSpan)timeout.GetValue(worker)!).TotalSeconds;
            if (actual != expected)
                throw new Exception($"Setting {setting ?? "<unset>"}: expected {expected}, found {actual}");
            Console.WriteLine($"PASS {setting ?? "<unset>"}: {actual}s");
        }
        finally
        {
            ((IDisposable)completed.GetValue(worker)!).Dispose();
        }
    }
}
finally
{
    Environment.SetEnvironmentVariable(key, previous);
}
